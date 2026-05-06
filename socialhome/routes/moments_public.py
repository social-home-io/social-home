"""SH-side routes for public-Momentum management (§Momentum-public).

Auth-gated REST surface that lets a user manage their own GFS
registrations + follows. The heavy lifting lives in
:class:`MomentPublicService`; this file only translates HTTP to
method calls.

Routes:

* ``GET    /api/moments/public/registrations`` — list GFSes the
  caller is currently registered on.
* ``POST   /api/moments/public/registrations`` — body
  ``{gfs_id, default_share?}``; signs + posts to the GFS.
* ``DELETE /api/moments/public/registrations/{gfs_id}`` — deregister.
* ``PATCH  /api/moments/public/registrations/{gfs_id}`` — body
  ``{default_share}``; flip the per-user default.
* ``GET    /api/moments/public/follows`` — caller's GFS follows.
* ``POST   /api/moments/public/follows`` — body
  ``{gfs_id, followed_user_id}``; signs + posts to the GFS.
* ``DELETE /api/moments/public/follows/{gfs_id}/{user_id}``.
* ``GET    /api/gfs/{gfs_id}/users`` — proxy the GFS directory.
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import moment_public_service_key
from ..services.moment_public_service import MomentPublicError
from .base import BaseView


class MomentPublicRegistrationCollectionView(BaseView):
    async def get(self) -> web.Response:
        svc = self.svc(moment_public_service_key)
        regs = await svc.list_registrations(self.user.user_id)
        return self._json(
            [
                {
                    "user_id": r.user_id,
                    "gfs_id": r.gfs_id,
                    "registered_at": r.registered_at,
                    "default_share": r.default_share,
                }
                for r in regs
            ]
        )

    async def post(self) -> web.Response:
        svc = self.svc(moment_public_service_key)
        body = await self.body()
        gfs_id = str(body.get("gfs_id") or "")
        if not gfs_id:
            raise web.HTTPBadRequest(text='{"error":"gfs_id_required"}')
        default_share = bool(body.get("default_share", True))
        try:
            reg = await svc.register(
                user_id=self.user.user_id,
                gfs_id=gfs_id,
                default_share=default_share,
            )
        except MomentPublicError as exc:
            raise web.HTTPBadGateway(text=f'{{"error":"{exc!s}"}}') from exc
        return self._json(
            {
                "user_id": reg.user_id,
                "gfs_id": reg.gfs_id,
                "registered_at": reg.registered_at,
                "default_share": reg.default_share,
            },
            status=201,
        )


class MomentPublicRegistrationDetailView(BaseView):
    async def delete(self) -> web.Response:
        svc = self.svc(moment_public_service_key)
        await svc.deregister(user_id=self.user.user_id, gfs_id=self.match("gfs_id"))
        return web.Response(status=204)

    async def patch(self) -> web.Response:
        svc = self.svc(moment_public_service_key)
        body = await self.body()
        if "default_share" not in body:
            raise web.HTTPBadRequest(text='{"error":"default_share_required"}')
        await svc.set_default_share(
            user_id=self.user.user_id,
            gfs_id=self.match("gfs_id"),
            default_share=bool(body["default_share"]),
        )
        return web.Response(status=204)


class MomentPublicFollowCollectionView(BaseView):
    async def get(self) -> web.Response:
        svc = self.svc(moment_public_service_key)
        follows = await svc.list_follows(self.user.user_id)
        return self._json(
            [
                {
                    "follower_user_id": f.follower_user_id,
                    "followed_user_id": f.followed_user_id,
                    "gfs_id": f.gfs_id,
                    "followed_username": f.followed_username,
                    "followed_display_name": f.followed_display_name,
                    "created_at": f.created_at,
                }
                for f in follows
            ]
        )

    async def post(self) -> web.Response:
        svc = self.svc(moment_public_service_key)
        body = await self.body()
        gfs_id = str(body.get("gfs_id") or "")
        followed_user_id = str(body.get("followed_user_id") or "")
        if not gfs_id or not followed_user_id:
            raise web.HTTPBadRequest(
                text='{"error":"gfs_id_and_followed_user_id_required"}'
            )
        try:
            follow = await svc.follow(
                follower_user_id=self.user.user_id,
                gfs_id=gfs_id,
                followed_user_id=followed_user_id,
            )
        except MomentPublicError as exc:
            raise web.HTTPBadGateway(text=f'{{"error":"{exc!s}"}}') from exc
        return self._json(
            {
                "follower_user_id": follow.follower_user_id,
                "followed_user_id": follow.followed_user_id,
                "gfs_id": follow.gfs_id,
                "followed_username": follow.followed_username,
                "followed_display_name": follow.followed_display_name,
                "created_at": follow.created_at,
            },
            status=201,
        )


class MomentPublicFollowDetailView(BaseView):
    async def delete(self) -> web.Response:
        svc = self.svc(moment_public_service_key)
        await svc.unfollow(
            follower_user_id=self.user.user_id,
            gfs_id=self.match("gfs_id"),
            followed_user_id=self.match("user_id"),
        )
        return web.Response(status=204)


class GfsUserDirectoryProxyView(BaseView):
    async def get(self) -> web.Response:
        svc = self.svc(moment_public_service_key)
        try:
            users = await svc.fetch_directory(self.match("gfs_id"))
        except MomentPublicError as exc:
            raise web.HTTPBadGateway(text=f'{{"error":"{exc!s}"}}') from exc
        return self._json({"users": users})
