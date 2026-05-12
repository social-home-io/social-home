"""Home Assistant Supervisor API client.

A thin wrapper for the Supervisor-only endpoints used by
:class:`HaBootstrap` when Social Home runs as a HA add-on:

* ``GET /auth/list``  — discover the HA owner account so we can provision
  them as the initial Social Home admin.
* ``GET /addons/self/info`` — read our own add-on metadata so the
  discovery payload can advertise a reachable ``host`` + ``port`` for
  the integration. The Supervisor rewrites underscores in the slug
  to dashes when it assigns Docker DNS names, so handing that
  hostname over directly spares the integration the substitution
  dance.
* ``POST /discovery`` — register the add-on with HA's discovery integration
  so the official ``socialhome`` HA integration can pick us up automatically.

The Supervisor sets ``SUPERVISOR_URL`` / ``SUPERVISOR_TOKEN`` in the add-on
environment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HaUser:
    """A Home Assistant user record as returned by Supervisor ``/auth/list``.

    Carries the **auth-provider username** (the credential the operator
    types at the HA login screen) plus the **display name** HA renders
    for the person — distinct concepts that the previous code path
    conflated by deriving an entity slug from the username and looking
    up ``person.<username>`` in the state machine. Issue #297 documents
    the symptom; the fix is to stop guessing the slug and trust the
    fields ``/auth/list`` already returns.
    """

    username: str
    name: str
    is_owner: bool

    @classmethod
    def from_dict(cls, data: dict) -> HaUser | None:
        """Return a :class:`HaUser` from the Supervisor payload, or
        ``None`` when the entry is system-generated or missing the
        username field. Supervisor includes service accounts (e.g. the
        cloud / mobile-app bridges) in ``/auth/list`` with
        ``system_generated: true``; those are uninteresting to the
        provisioning wizard."""
        if data.get("system_generated", False):
            return None
        username = data.get("username")
        if not username:
            return None
        return cls(
            username=str(username),
            name=str(data.get("name") or username),
            is_owner=bool(data.get("is_owner", False)),
        )


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

    async def list_users(self) -> list[HaUser]:
        """Return every non-system HA user from ``/auth/list``.

        Supervisor's response envelope is ``{"data": {"users": [...]}}``
        as of HA 2024+; the older ``{"users": [...]}`` shape is tolerated.
        Filters out ``system_generated: true`` entries (cloud / mobile-app
        service accounts) — the wizard wants real human accounts only.
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
            return []
        users_raw = data.get("data", data).get("users", [])
        out: list[HaUser] = []
        for entry in users_raw:
            user = HaUser.from_dict(entry)
            if user is not None:
                out.append(user)
        return out

    async def get_owner(self) -> HaUser | None:
        """Return the HA owner (``is_owner: true``) or ``None``.

        Carries username + display name so the provisioning wizard can
        mirror the admin without a ``person.*`` round-trip — see #297.
        """
        for user in await self.list_users():
            if user.is_owner:
                return user
        log.warning("supervisor: no owner found in /auth/list")
        return None

    async def get_owner_username(self) -> str | None:
        """Back-compat shim returning just the owner's username.

        Bootstrap (``HaBootstrap.run``) and the admin-picture sync still
        consume a bare string; switching them is a separate cleanup.
        Wizard provisioning has moved to :meth:`get_owner` so it gets
        the display name in the same round-trip.
        """
        owner = await self.get_owner()
        return owner.username if owner else None

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


__all__ = ["AddonInfo", "HaUser", "SupervisorClient"]
