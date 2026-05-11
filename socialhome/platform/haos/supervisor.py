"""Home Assistant Supervisor API client.

A thin wrapper for the Supervisor-only endpoints used by
:class:`HaBootstrap` when Social Home runs as a HA add-on. Two of
the three calls go through the official
:mod:`aiohasupervisor` client — typed models, structured errors,
matching versioning with the Supervisor — and the third (``/auth/list``,
not yet exposed by the library) stays as a raw ``aiohttp`` GET.

Endpoints used:

* ``GET /auth/list``  — discover the HA owner account so we can
  provision them as the initial Social Home admin. Raw aiohttp; no
  library coverage yet.
* ``GET /addons/self/info`` — read our own add-on metadata so the
  discovery payload can advertise a reachable ``host`` + ``port``
  for the integration. The Supervisor rewrites ``_`` in the slug to
  ``-`` when it assigns Docker DNS names, so handing that hostname
  over directly spares the integration the substitution dance.
  Goes through :class:`aiohasupervisor.SupervisorClient`.
* ``POST /discovery`` — register the add-on with HA's discovery
  integration so the official ``socialhome`` HA integration can
  pick us up automatically. Goes through
  :class:`aiohasupervisor.SupervisorClient`.

The Supervisor sets ``SUPERVISOR_URL`` / ``SUPERVISOR_TOKEN`` in
the add-on environment.
"""

from __future__ import annotations

import logging

import aiohttp
from aiohasupervisor import SupervisorClient as _AhaSupervisorClient
from aiohasupervisor import SupervisorError
from aiohasupervisor.models.addons import InstalledAddonComplete
from aiohasupervisor.models.discovery import DiscoveryConfig

log = logging.getLogger(__name__)


class SupervisorClient:
    """HTTP client for the Supervisor API (never talks to HA Core)."""

    __slots__ = ("_session", "_base_url", "_token", "_aha")

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        token: str,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._token = token
        # ``aiohasupervisor`` shares our aiohttp session so the
        # Supervisor sees one connection pool, not two.
        self._aha = _AhaSupervisorClient(self._base_url, token, session=session)

    @property
    def base_url(self) -> str:
        return self._base_url

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def get_owner_username(self) -> str | None:
        """Return the non-system HA owner, or ``None``.

        Uses ``GET /auth/list`` directly via ``aiohttp`` because
        ``aiohasupervisor`` 0.4.x does not yet expose an auth client.
        The response envelope is ``{"data": {"users": [...]}}`` as
        of HA 2024+; the older ``{"users": [...]}`` shape is
        tolerated.
        """
        try:
            async with self._session.get(
                f"{self._base_url}/auth/list",
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except aiohttp.ClientError as exc:
            log.warning("supervisor: /auth/list failed: %s", exc)
            return None

        users = data.get("data", data).get("users", [])
        owner = next(
            (
                u
                for u in users
                if u.get("is_owner") and not u.get("system_generated", False)
            ),
            None,
        )
        if not owner:
            log.warning("supervisor: no owner found in /auth/list")
            return None
        return owner.get("username") or owner.get("name")

    async def get_self_info(self) -> InstalledAddonComplete | None:
        """Return ``GET /addons/self/info`` as a typed model, or ``None``.

        Used by :meth:`HaBootstrap._push_discovery` so the payload
        can advertise the add-on's reachable ``host`` (from
        ``info.hostname``) and ``port`` (from ``info.ingress_port``).
        ``hostname`` has already had ``_`` replaced with ``-`` by
        the Supervisor (Docker DNS doesn't accept underscores), so
        the integration uses it verbatim.
        """
        try:
            return await self._aha.addons.addon_info("self")
        except SupervisorError as exc:
            log.warning("supervisor: /addons/self/info failed: %s", exc)
            return None

    async def push_discovery(self, service: str, config: dict) -> bool:
        """POST ``/discovery`` via ``aiohasupervisor``.

        ``service`` is the HA discovery service name (always
        ``"socialhome"`` for us); ``config`` is the payload the HA
        integration's ``async_step_discovery`` consumes (host, port,
        integration token). Returns ``True`` on success, ``False``
        on any Supervisor error (logged).
        """
        try:
            await self._aha.discovery.set(
                DiscoveryConfig(service=service, config=config),
            )
            return True
        except SupervisorError as exc:
            log.warning("supervisor: discovery push failed: %s", exc)
            return False


__all__ = ["SupervisorClient"]
