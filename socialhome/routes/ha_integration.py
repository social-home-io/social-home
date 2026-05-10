"""HA integration bridge routes (§7, §11).

Endpoints the separate `ha-integration` HACS package calls into — the
integration runs inside Home Assistant, knows the externally-reachable
URL (admin-set `external_url` or Nabu Casa Remote UI), and pushes it
here so the addon can stamp it into new pairing QRs and notify already-
paired peers via ``URL_UPDATED``.

Auth: uses the normal bearer-token path. The integration holds the
token written to ``<data_dir>/integration_token.txt`` by
:class:`~socialhome.platform.ha.bootstrap.HaBootstrap` on first boot.
Admin-only — the integration owner is always the HA owner provisioned
as an SH admin during bootstrap.

Routes registered here:

* ``PUT /api/ha/integration/federation-base`` — upsert the base URL.
  Fans out ``URL_UPDATED`` to every confirmed peer if the value
  changed.
* ``GET /api/ha/integration/federation-base`` — read-only mirror so
  the integration can verify current state on re-bind.
* ``PUT /api/ha/integration/ice-servers`` — upsert the WebRTC
  ICE-server list (operator-managed STUN/TURN, typically Nabu Casa's
  managed TURN). Propagates immediately to the live
  :class:`FederationService` so future peer handshakes pick it up.
* ``GET /api/ha/integration/ice-servers`` — read-only mirror.
"""

from __future__ import annotations

import json
import logging

from aiohttp import web

from ..app_keys import db_key, federation_service_key, url_update_outbound_key
from ..security import error_response
from .base import BaseView

log = logging.getLogger(__name__)


_INSTANCE_CONFIG_KEY = "ha_federation_base"
_INSTANCE_CONFIG_KEY_ICE = "ha_ice_servers"

_ALLOWED_ICE_SCHEMES = ("stun:", "stuns:", "turn:", "turns:")
_ICE_MAX_SERVERS = 8
_ICE_MAX_URLS_PER_SERVER = 4
_ICE_MAX_FIELD_LEN = 512


def _validate_base(raw: str) -> str | None:
    """Normalize + validate a pushed base URL. Return the cleaned URL
    or ``None`` if it fails sanity checks.
    """
    base = raw.strip().rstrip("/")
    if not base:
        return None
    if not (base.startswith("http://") or base.startswith("https://")):
        return None
    return base


def _validate_ice_servers(raw) -> list[dict] | None:
    """Validate a pushed ICE-server list and return a normalized copy.

    Accepted shape — Chrome / aiolibdatachannel-compatible::

        [
            {"urls": ["stun:stun.example:3478"]},
            {
                "urls": ["turn:turn.example:3478", "turns:turn.example:5349"],
                "username": "u",
                "credential": "p",
            },
        ]

    Return ``None`` for any malformed input. Length / count limits are
    bounded so the integration cannot push a payload that bloats every
    outbound ``ice_servers`` echo (§24.10.7) or peer config.
    """
    if not isinstance(raw, list):
        return None
    if len(raw) > _ICE_MAX_SERVERS:
        return None
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return None
        urls = entry.get("urls")
        # Chrome-compatible: "urls" can be either a string or a list of
        # strings. Normalize to list[str] so downstream code (the
        # transport layer flattens it again) sees a single shape.
        if isinstance(urls, str):
            urls = [urls]
        if not isinstance(urls, list) or not urls:
            return None
        if len(urls) > _ICE_MAX_URLS_PER_SERVER:
            return None
        cleaned_urls: list[str] = []
        for url in urls:
            if not isinstance(url, str):
                return None
            url = url.strip()
            if not url or len(url) > _ICE_MAX_FIELD_LEN:
                return None
            if not url.lower().startswith(_ALLOWED_ICE_SCHEMES):
                return None
            cleaned_urls.append(url)
        normalized: dict = {"urls": cleaned_urls}
        for opt in ("username", "credential"):
            val = entry.get(opt)
            if val is None:
                continue
            if not isinstance(val, str) or len(val) > _ICE_MAX_FIELD_LEN:
                return None
            normalized[opt] = val
        out.append(normalized)
    return out


class HaIntegrationFederationBaseView(BaseView):
    """``GET / PUT /api/ha/integration/federation-base``.

    The HA integration POSTs here with ``{"base": "https://..."}`` after
    resolving the externally-reachable URL (Nabu Casa Remote UI or HA
    ``external_url``). We persist the value in ``instance_config``
    where :meth:`HomeAssistantAdapter.get_federation_base` reads it,
    and — when the value differs from the last seen one — fan out
    ``URL_UPDATED`` to every confirmed peer so their
    ``remote_inbox_url`` tracks the move.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        db = self.svc(db_key)
        row = await db.fetchone(
            "SELECT value FROM instance_config WHERE key=?",
            (_INSTANCE_CONFIG_KEY,),
        )
        base = str(row["value"]) if row is not None else None
        return web.json_response({"base": base})

    async def put(self) -> web.Response:
        ctx = self.user
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        body = await self.body()
        raw = str(body.get("base") or "")
        cleaned = _validate_base(raw)
        if cleaned is None:
            return error_response(
                422,
                "UNPROCESSABLE",
                "base must be a non-empty http(s) URL.",
            )

        db = self.svc(db_key)
        previous_row = await db.fetchone(
            "SELECT value FROM instance_config WHERE key=?",
            (_INSTANCE_CONFIG_KEY,),
        )
        previous = str(previous_row["value"]) if previous_row is not None else None

        await db.enqueue(
            "INSERT INTO instance_config(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_INSTANCE_CONFIG_KEY, cleaned),
        )

        notified = 0
        if previous != cleaned:
            outbound = self.svc(url_update_outbound_key)
            try:
                notified = await outbound.publish(new_inbox_base_url=cleaned)
            except Exception:  # pragma: no cover — defensive
                log.exception("ha_integration: URL_UPDATED fan-out failed")

        return web.json_response(
            {
                "ok": True,
                "base": cleaned,
                "changed": previous != cleaned,
                "peers_notified": notified,
            }
        )


def _load_ice_servers(raw_value: str | None) -> list[dict]:
    """Decode the persisted JSON blob, defending against bad rows.

    Bad JSON or a value that no longer matches the validator (schema
    drift, hand-edited DB) returns ``[]`` rather than raising — the
    instance keeps booting with no ICE servers, and the operator gets
    a chance to re-push from the integration.
    """
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
    except (TypeError, ValueError):
        log.warning("ha_integration: persisted ice_servers JSON is malformed")
        return []
    cleaned = _validate_ice_servers(parsed)
    if cleaned is None:
        log.warning("ha_integration: persisted ice_servers failed re-validation")
        return []
    return cleaned


async def load_persisted_ice_servers(db) -> list[dict]:
    """Read the operator-pushed ICE-server list from ``instance_config``.

    Called by ``app._on_startup`` after the DB is open so the live
    :class:`FederationService` and its transport see the persisted
    list before the first peer handshake.
    """
    row = await db.fetchone(
        "SELECT value FROM instance_config WHERE key=?",
        (_INSTANCE_CONFIG_KEY_ICE,),
    )
    return _load_ice_servers(str(row["value"]) if row is not None else None)


class HaIntegrationIceServersView(BaseView):
    """``GET / PUT /api/ha/integration/ice-servers``.

    The HA integration POSTs ``{"ice_servers": [...]}`` after the
    operator picks a STUN/TURN provider in the integration config flow
    (typically a Nabu Casa managed TURN credential). We:

    1. Validate the payload (Chrome-compatible shape, scheme allow-list).
    2. Persist as JSON in ``instance_config`` so the next boot replays
       the same list before any peer handshake fires.
    3. Hand the list to :meth:`FederationService.set_ice_servers`,
       which propagates to the attached :class:`FederationTransport` —
       new DataChannel handshakes pick up the operator's TURN config
       immediately. Existing peers keep their previous config (live
       renegotiation is out of scope; reconnects pick up the new list).

    No federation event is emitted: ICE servers are local config, not
    something we tell remote peers about. Peers learn each operator's
    chosen STUN/TURN through the §24.10.7 ``ice_servers`` echo on their
    own connection, not by us pushing it to them.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        db = self.svc(db_key)
        servers = await load_persisted_ice_servers(db)
        return web.json_response({"ice_servers": servers})

    async def put(self) -> web.Response:
        ctx = self.user
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        body = await self.body()
        cleaned = _validate_ice_servers(body.get("ice_servers"))
        if cleaned is None:
            return error_response(
                422,
                "UNPROCESSABLE",
                "ice_servers must be a list of "
                "{urls: [stun|turn|stuns|turns:...], username?, credential?}.",
            )

        db = self.svc(db_key)
        encoded = json.dumps(cleaned, separators=(",", ":"))
        previous_row = await db.fetchone(
            "SELECT value FROM instance_config WHERE key=?",
            (_INSTANCE_CONFIG_KEY_ICE,),
        )
        previous = str(previous_row["value"]) if previous_row is not None else None

        await db.enqueue(
            "INSERT INTO instance_config(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_INSTANCE_CONFIG_KEY_ICE, encoded),
        )

        federation = self.svc(federation_service_key)
        federation.set_ice_servers(cleaned)

        return web.json_response(
            {
                "ok": True,
                "ice_servers": cleaned,
                "changed": previous != encoded,
            }
        )
