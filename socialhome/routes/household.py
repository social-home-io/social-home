"""Household routes — preferences + household name (§22)."""

from __future__ import annotations

from dataclasses import asdict

from aiohttp import web

from .. import app_keys as K
from .base import BaseView


class HouseholdPreferencesView(BaseView):
    """``GET /api/household/preferences`` + ``PUT /api/household/preferences``."""

    async def get(self) -> web.Response:
        self.user
        svc = self.svc(K.preferences_service_key)
        prefs = await svc.get_household()
        return self._json(asdict(prefs))

    async def put(self) -> web.Response:
        ctx = self.user
        body = await self.body()
        svc = self.svc(K.preferences_service_key)
        prefs = await svc.update_household(
            actor_is_admin=ctx.is_admin,
            household_name=body.get("household_name"),
            toggles=body.get("toggles"),
            tz=body.get("tz"),
        )
        return self._json(asdict(prefs))
