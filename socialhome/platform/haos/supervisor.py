"""Home Assistant Supervisor API client.

A thin wrapper for the Supervisor-only endpoints used by
:class:`HaBootstrap` when Social Home runs as a HA add-on:

* ``GET /addons/self/info`` — read our own add-on metadata so the
  discovery payload can advertise a reachable ``host`` + ``port`` for
  the integration. The Supervisor rewrites underscores in the slug
  to dashes when it assigns Docker DNS names, so handing that
  hostname over directly spares the integration the substitution
  dance.
* ``POST /discovery`` — register the add-on with HA's discovery integration
  so the official ``socialhome`` HA integration can pick us up automatically.

User discovery (the owner account, the picker for the wizard, the
ingress-header → identity resolution) goes through HA Core's WS
``config/auth/list`` instead — see
:meth:`socialhome.platform.ha.client.HaClient.list_auth_users`.
The Supervisor's REST ``/auth/list`` was a second source of identity
data with a strictly smaller payload (no ``id``, no
``credentials``) and the same envelope; keeping it forked invited
the kind of slug-vs-username gotchas issue #297 documents.

The Supervisor sets ``SUPERVISOR_URL`` / ``SUPERVISOR_TOKEN`` in the
add-on environment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AddonInfo:
    """Subset of ``GET /addons/self/info`` that we actually consume.

    The Supervisor response carries 40+ fields; we only need the
    DNS hostname and the ingress port to build the discovery
    payload. Modelling just those keeps the surface easy to type
    and easy to fake in tests without re-creating the whole
    upstream schema.
    """

    hostname: str
    ingress_port: int

    @classmethod
    def from_response(cls, data: dict | None) -> AddonInfo | None:
        """Return an :class:`AddonInfo` or ``None`` if either required
        field is missing from the Supervisor's response."""
        if not data:
            return None
        hostname = data.get("hostname")
        port = data.get("ingress_port")
        if not hostname or port is None:
            return None
        return cls(hostname=str(hostname), ingress_port=int(port))


class SupervisorClient:
    """HTTP client for the Supervisor API (never talks to HA Core)."""

    __slots__ = ("_session", "_base_url", "_token")

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        token: str,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._token = token

    @property
    def base_url(self) -> str:
        return self._base_url

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def get_self_info(self) -> AddonInfo | None:
        """Return ``GET /addons/self/info`` as a typed :class:`AddonInfo`,
        or ``None`` if the call failed / the response was missing
        the fields :class:`HaBootstrap._push_discovery` needs.

        ``AddonInfo.hostname`` has already had ``_`` replaced with
        ``-`` by the Supervisor (Docker DNS doesn't accept
        underscores), so the integration can use it verbatim
        without further substitution.
        """
        try:
            async with self._session.get(
                f"{self._base_url}/addons/self/info",
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except aiohttp.ClientError as exc:
            log.warning("supervisor: /addons/self/info failed: %s", exc)
            return None
        return AddonInfo.from_response(data.get("data"))

    async def push_discovery(self, payload: dict) -> bool:
        """POST ``/discovery`` — returns ``True`` on 2xx.

        The payload should carry the integration token plus the
        ``host`` + ``port`` the HA integration needs to reach us.
        :class:`HaBootstrap` builds it.
        """
        try:
            async with self._session.post(
                f"{self._base_url}/discovery",
                headers=self._headers(),
                json=payload,
            ) as resp:
                if 200 <= resp.status < 300:
                    return True
                log.warning(
                    "supervisor: discovery push returned HTTP %d",
                    resp.status,
                )
                return False
        except aiohttp.ClientError as exc:
            log.warning("supervisor: discovery push failed: %s", exc)
            return False


__all__ = ["AddonInfo", "SupervisorClient"]
