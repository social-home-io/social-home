"""GFS federation service — instance registration, event relay, subscriptions.

Business logic only — all SQL lives in :mod:`.repositories`. Crypto
helpers are reused from :mod:`socialhome.crypto` (no duplication).

Fan-out delivery is **WebSocket-primary, HTTPS-fallback** (spec §24.12):
if a paired SH instance has an open ``/gfs/ws`` WebSocket, the event is
pushed over that connection; otherwise it falls back to an HTTPS POST
to the subscriber's inbox URL.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import aiohttp

from ..crypto import b64url_decode, verify_ed25519
from .domain import ClientInstance, GfsSubscriber, GlobalSpace
from .repositories import AbstractGfsFederationRepo

if TYPE_CHECKING:
    from .ws_registry import GfsWebSocketRegistry

log = logging.getLogger(__name__)


class GfsFederationService:
    """Lightweight federation relay for the GFS process.

    Responsible for:
    * Registering/updating client household instances.
    * Verifying Ed25519 signatures on inbound publish requests.
    * Fanning out events to all subscribers (WS push, HTTPS fallback).
    * Managing space subscription lists.
    * Listing all known global spaces.
    """

    __slots__ = ("_repo", "_ws_registry")

    def __init__(
        self,
        repo: AbstractGfsFederationRepo,
        ws_registry: "GfsWebSocketRegistry | None" = None,
    ) -> None:
        self._repo = repo
        self._ws_registry = ws_registry

    async def register_instance(
        self,
        instance_id: str,
        public_key: str,
        inbox_url: str,
        *,
        display_name: str = "",
        auto_accept: bool = False,
    ) -> None:
        """Register or update a client household instance."""
        await self._repo.upsert_instance(
            ClientInstance(
                instance_id=instance_id,
                display_name=display_name,
                public_key=public_key,
                inbox_url=inbox_url,
                status="active" if auto_accept else "pending",
                auto_accept=auto_accept,
            )
        )
        log.debug("GFS: registered instance %s inbox=%s", instance_id, inbox_url)

    async def publish_event(
        self,
        space_id: str,
        event_type: str,
        payload: object,
        from_instance: str,
        signature: str = "",
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> list[str]:
        """Relay an event to all subscribers of *space_id*.

        Validates the Ed25519 *signature* using the public key registered
        for *from_instance*. Returns the list of instance_ids successfully
        notified.

        Raises :class:`PermissionError` when *from_instance* is unknown or
        the signature is invalid.
        """
        inst = await self._repo.get_instance(from_instance)
        if inst is None:
            raise PermissionError(f"Unknown instance: {from_instance}")

        if signature:
            canonical = json.dumps(
                {
                    "space_id": space_id,
                    "event_type": event_type,
                    "payload": payload,
                    "from_instance": from_instance,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            raw_key = bytes.fromhex(inst.public_key)
            raw_sig = b64url_decode(signature)
            if not verify_ed25519(raw_key, canonical, raw_sig):
                raise PermissionError("Invalid Ed25519 signature")

        # Preserve existing row data if present; otherwise create a minimal
        # pending row. Admin portal fleshes the metadata out on accept.
        existing = await self._repo.get_space(space_id)
        if existing is None:
            await self._repo.upsert_space(
                GlobalSpace(
                    space_id=space_id,
                    owning_instance=from_instance,
                )
            )

        subscribers = await self._repo.list_subscribers(
            space_id,
            exclude=from_instance,
        )

        event_body = {
            "space_id": space_id,
            "event_type": event_type,
            "payload": payload,
            "from_instance": from_instance,
        }

        return await self._fan_out(subscribers, event_body, session)

    async def subscribe(self, instance_id: str, space_id: str) -> None:
        """Add *instance_id* as a subscriber of *space_id*."""
        existing = await self._repo.get_space(space_id)
        if existing is None:
            # Subscription precedes publish — create a pending row so the
            # admin can see the demand.
            await self._repo.upsert_space(
                GlobalSpace(
                    space_id=space_id,
                    owning_instance=instance_id,
                )
            )
        await self._repo.add_subscriber(
            space_id=space_id,
            instance_id=instance_id,
        )
        log.debug("GFS: %s subscribed to space %s", instance_id, space_id)

    async def unsubscribe(self, instance_id: str, space_id: str) -> None:
        """Remove *instance_id* from subscribers of *space_id*."""
        await self._repo.remove_subscriber(
            space_id=space_id,
            instance_id=instance_id,
        )
        log.debug("GFS: %s unsubscribed from space %s", instance_id, space_id)

    async def list_spaces(
        self,
        *,
        status: str | None = None,
    ) -> list[GlobalSpace]:
        """Return global/public spaces known to this GFS node.

        The public ``GET /gfs/spaces`` endpoint passes ``status='active'``
        to hide pending + banned rows. Internal callers (admin, tests)
        can pass ``status=None`` to see everything.
        """
        return await self._repo.list_spaces(status=status)

    # ── Fan-out ──────────────────────────────────────────────────────────

    async def _fan_out(
        self,
        subscribers: list[GfsSubscriber],
        event_body: dict,
        session: aiohttp.ClientSession | None,
    ) -> list[str]:
        """Deliver *event_body* to each subscriber.

        Tries the SH↔GFS WebSocket first (push frame ``{type:"relay", ...}``).
        If no socket is registered for the subscriber or the send fails,
        falls back to an HTTPS POST to the subscriber's inbox URL.
        """
        own_session = session is None
        active: aiohttp.ClientSession = (
            session if session is not None else aiohttp.ClientSession()
        )
        push_frame = {"type": "relay", **event_body}
        try:
            delivered: list[str] = []
            for sub in subscribers:
                # WebSocket push first.
                if self._ws_registry is not None and await self._ws_registry.send(
                    sub.instance_id,
                    push_frame,
                ):
                    delivered.append(sub.instance_id)
                    continue

                # HTTPS-inbox fallback.
                try:
                    async with active.post(
                        sub.inbox_url,
                        json=event_body,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status < 400:
                            delivered.append(sub.instance_id)
                        else:
                            log.warning(
                                "GFS fan-out: %s returned HTTP %s",
                                sub.inbox_url,
                                resp.status,
                            )
                except Exception as exc:
                    log.warning(
                        "GFS fan-out: failed to deliver to %s: %s",
                        sub.inbox_url,
                        exc,
                    )
            return delivered
        finally:
            if own_session:
                await active.close()
