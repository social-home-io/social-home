"""First-boot setup routes — the wizard's three mode-specific endpoints.

* ``POST /api/setup/standalone`` — operator submits ``{username, password}``;
  we seed ``platform_users`` + ``users`` and mark setup complete.
* ``GET  /api/setup/ha/persons`` — list HA persons so the operator can
  pick which one becomes the SH owner.
* ``POST /api/setup/ha/owner`` — operator submits ``{username}`` for the
  picked HA person; we mirror them into ``users`` as admin and mark
  setup complete. ha-mode auth runs through HA (X-Ingress-User or HA
  bearer tokens), so no password is needed at this step.
* ``POST /api/setup/haos/complete`` — no body. Reads the HA owner from
  ``http://supervisor/auth/list``, mirrors them, and marks setup
  complete.

Every endpoint is a public path while ``setup_required`` is true; once
complete, they all return 409 ``ALREADY_COMPLETE``. The SPA consults
``GET /api/instance/config`` before showing the wizard, so it should
never hit the gate in practice — the gate is defence-in-depth.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..app_keys import (
    config_key,
    db_key,
    platform_adapter_key,
    setup_service_key,
)
from ..platform.adapter import Capability, ExternalUser
from ..security import error_response
from .base import BaseView

log = logging.getLogger(__name__)


async def _gate(view: BaseView) -> web.Response | None:
    """Return a 409 response if setup is already complete, else ``None``.

    Centralised so each handler can short-circuit with a single line:
    ``if (resp := await _gate(self)): return resp``.
    """
    setup = view.svc(setup_service_key)
    if not await setup.is_required():
        return error_response(
            409,
            "ALREADY_COMPLETE",
            "First-boot setup has already been completed.",
        )
    return None


class StandaloneSetupView(BaseView):
    """``POST /api/setup/standalone`` — set the admin username + password.

    Returns ``{token}`` (status 201) so the SPA can drop straight into
    the app authenticated, with no second login round-trip.
    """

    async def post(self) -> web.Response:
        if (resp := await _gate(self)) is not None:
            return resp
        config = self.svc(config_key)
        if config.mode != "standalone":
            return error_response(
                409,
                "WRONG_MODE",
                f"This endpoint is for standalone mode (current: {config.mode}).",
            )
        body = await self.body()
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        if not username or not password:
            return error_response(
                422,
                "UNPROCESSABLE",
                "username and password are required.",
            )
        adapter = self.svc(platform_adapter_key)
        provision = getattr(adapter, "provision_admin", None)
        if provision is None:
            return error_response(
                500,
                "INTERNAL_ERROR",
                "Standalone adapter is missing provision_admin.",
            )
        await provision(username=username, password=password)
        await self.svc(setup_service_key).mark_complete()
        token = await adapter.issue_bearer_token(username, password)
        return web.json_response({"token": token}, status=201)


class HaPersonsSetupView(BaseView):
    """``GET /api/setup/ha/persons`` — list HA persons for the wizard."""

    async def get(self) -> web.Response:
        if (resp := await _gate(self)) is not None:
            return resp
        config = self.svc(config_key)
        if config.mode not in ("ha", "haos"):
            return error_response(
                409,
                "WRONG_MODE",
                f"This endpoint is for ha/haos modes (current: {config.mode}).",
            )
        adapter = self.svc(platform_adapter_key)
        persons = await adapter.users.list_users()
        return web.json_response(
            {
                "persons": [
                    {
                        "username": p.username,
                        "display_name": p.display_name,
                        "picture_url": p.picture_url,
                    }
                    for p in persons
                ]
            }
        )


class HaOwnerSetupView(BaseView):
    """``POST /api/setup/ha/owner`` — operator picks the HA owner.

    No password collected: ha mode authenticates via X-Ingress-User
    (when behind a proxy) or HA long-lived access tokens. Local
    password auth in ha mode is a separate follow-up.
    """

    async def post(self) -> web.Response:
        if (resp := await _gate(self)) is not None:
            return resp
        config = self.svc(config_key)
        if config.mode != "ha":
            return error_response(
                409,
                "WRONG_MODE",
                f"This endpoint is for ha mode (current: {config.mode}).",
            )
        body = await self.body()
        username = str(body.get("username") or "").strip()
        if not username:
            return error_response(
                422,
                "UNPROCESSABLE",
                "username is required.",
            )
        adapter = self.svc(platform_adapter_key)
        external = await adapter.users.get(username)
        if external is None:
            return error_response(
                422,
                "UNPROCESSABLE",
                f"No Home Assistant person found with username {username!r}.",
            )
        await _mirror_admin_user(self.svc(db_key), external)
        await self.svc(setup_service_key).mark_complete()
        return web.Response(status=204)


class HaosCompleteSetupView(BaseView):
    """``POST /api/setup/haos/complete`` — read the owner from Supervisor.

    Idempotent. The SPA POSTs this silently on first load when
    ``mode == 'haos'`` and redirects to the app afterwards.
    """

    async def post(self) -> web.Response:
        if (resp := await _gate(self)) is not None:
            return resp
        config = self.svc(config_key)
        if config.mode != "haos":
            return error_response(
                409,
                "WRONG_MODE",
                f"This endpoint is for haos mode (current: {config.mode}).",
            )
        adapter = self.svc(platform_adapter_key)
        if Capability.INGRESS not in adapter.capabilities:
            return error_response(
                500,
                "INTERNAL_ERROR",
                "haos adapter is missing the INGRESS capability.",
            )
        sv_client = getattr(adapter, "_supervisor_client", None)
        if sv_client is None:
            return error_response(
                503,
                "SUPERVISOR_UNAVAILABLE",
                "Supervisor client not yet wired — try again after startup.",
            )
        owner = await sv_client.get_owner_username()
        if not owner:
            return error_response(
                422,
                "NO_OWNER",
                "Home Assistant Supervisor reported no owner user.",
            )
        external = await adapter.users.get(owner)
        if external is None:
            return error_response(
                422,
                "NO_OWNER",
                f"Supervisor owner {owner!r} has no person.* entity in HA.",
            )
        await _mirror_admin_user(self.svc(db_key), external)
        await self.svc(setup_service_key).mark_complete()
        return web.json_response({"username": owner})


async def _mirror_admin_user(db, external: ExternalUser) -> None:
    """Insert the picked HA person into ``users`` as admin (idempotent)."""
    user_id = f"uid-{external.username}"
    await db.enqueue(
        """
        INSERT INTO users(username, user_id, display_name, is_admin)
        VALUES(?, ?, ?, 1)
        ON CONFLICT(username) DO UPDATE SET is_admin=1
        """,
        (external.username, user_id, external.display_name or external.username),
    )
