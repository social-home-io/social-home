"""Admin routes for the household instance-ban list (§Momentum-relay-policy).

CRUD over :class:`HouseholdInstanceBan` rows. Only household admins
can mutate; reads are admin-only too (the ban list isn't user-facing).

Routes:

* ``GET    /api/admin/instance-bans``
* ``POST   /api/admin/instance-bans``           body: ``{instance_id, reason?}``
* ``DELETE /api/admin/instance-bans/{instance_id}``
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import household_instance_ban_repo_key
from ..security import error_response
from .base import BaseView


class InstanceBanCollectionView(BaseView):
    async def get(self) -> web.Response:
        if self.user is None or not self.user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        repo = self.svc(household_instance_ban_repo_key)
        rows = await repo.list_all()
        return self._json(
            [
                {
                    "instance_id": r.instance_id,
                    "banned_at": r.banned_at,
                    "reason": r.reason,
                }
                for r in rows
            ]
        )

    async def post(self) -> web.Response:
        if self.user is None or not self.user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        body = await self.body()
        instance_id = str(body.get("instance_id") or "").strip()
        if not instance_id:
            raise web.HTTPBadRequest(text='{"error":"instance_id_required"}')
        reason = body.get("reason")
        repo = self.svc(household_instance_ban_repo_key)
        await repo.add(
            instance_id=instance_id,
            reason=str(reason) if reason else None,
        )
        return web.Response(status=201)


class InstanceBanDetailView(BaseView):
    async def delete(self) -> web.Response:
        if self.user is None or not self.user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        repo = self.svc(household_instance_ban_repo_key)
        await repo.remove(self.match("instance_id"))
        return web.Response(status=204)
