"""Personal user-block routes — ``/api/blocks`` (§Privacy).

A user can voluntarily hide another user's content from their own view —
their stories, posts, DMs, presence, notifications, and friends list all
filter the blocked user out. Distinct from the parent-driven CP block
(``/api/cp/minors/{id}/blocks``), which is guardian-managed.

* ``GET    /api/blocks``               — list of users I have blocked.
* ``POST   /api/blocks``               — block a user_id.
* ``DELETE /api/blocks/{user_id}``     — unblock a user_id.

Every endpoint scopes to the authenticated user; one user cannot read
or modify another user's block list.
"""

from __future__ import annotations

from aiohttp import web

from .. import app_keys as K
from ..security import error_response
from .base import BaseView


class BlockCollectionView(BaseView):
    """``GET|POST /api/blocks`` — list / add the caller's blocks."""

    async def get(self) -> web.Response:
        if self.user is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        svc = self.svc(K.user_service_key)
        rows = await svc.list_blocked(self.user.username)
        return self._json({"blocks": rows})

    async def post(self) -> web.Response:
        if self.user is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        body = await self.body()
        target = str(body.get("user_id") or "").strip()
        if not target:
            return error_response(400, "BAD_REQUEST", "Missing user_id.")
        svc = self.svc(K.user_service_key)
        await svc.block(self.user.username, target)
        return self._json({"user_id": target}, status=201)


class BlockDetailView(BaseView):
    """``DELETE /api/blocks/{user_id}`` — remove a block from the caller's list."""

    async def delete(self) -> web.Response:
        if self.user is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        target = self.match("user_id")
        svc = self.svc(K.user_service_key)
        await svc.unblock(self.user.username, target)
        return self._json({"user_id": target})
