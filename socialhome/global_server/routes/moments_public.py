"""GFS public-Momentum routes (§Momentum-public).

Three surfaces:

* **Signed wire endpoints** for instance-side mutations:
  - ``POST /gfs/users/register`` — opt a user into the directory.
  - ``POST /gfs/users/{user_id}/deregister`` — remove a registration.
  - ``POST /gfs/users/{user_id}/follow`` — record a follower.
  - ``POST /gfs/users/{user_id}/unfollow`` — drop a follower.
  - ``POST /gfs/moments/publish`` — fan out a signed moment envelope.
  - ``POST /gfs/moments/delete`` — fan out a tombstone.
* **Public discovery** ``GET /gfs/users`` (JSON) — list of active
  registrations. Includes the home-instance pk so any follower can
  TOFU the verifier on first follow.
* **Public landing** ``GET /users`` (HTML) — lightweight rendered
  directory page so an operator can preview the GFS's audience.

All signed endpoints reuse :func:`_rtc_authenticate` so the same
middleware that gates the Highlights publish flow gates these.
"""

from __future__ import annotations

import logging

from aiohttp import web

from .. import app_keys as K
from .base import GfsBaseView
from .rtc import _rtc_authenticate

log = logging.getLogger(__name__)


# ─── Signed wire endpoints ─────────────────────────────────────────────────


class GfsUserRegisterView(GfsBaseView):
    """``POST /gfs/users/register`` — record a public-Momentum opt-in."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, sender_instance_id = result
        registry = self.svc(K.gfs_moment_public_registry_key)

        user_id = str(body.get("user_id") or "")
        instance_id = str(body.get("instance_id") or "")
        if not user_id or not instance_id:
            return web.json_response({"error": "user_id_required"}, status=422)
        if instance_id != sender_instance_id:
            # The signing instance must match the registration instance —
            # we don't let one paired SH register a user on behalf of
            # another.
            return web.json_response({"error": "instance_mismatch"}, status=403)
        username = str(body.get("username") or "")
        display_name = str(body.get("display_name") or username or user_id)
        home_pk = str(body.get("home_instance_pk") or "")
        if not home_pk:
            return web.json_response({"error": "home_instance_pk_required"}, status=422)
        picture_url = body.get("picture_url")
        reg = await registry.register_user(
            user_id=user_id,
            instance_id=instance_id,
            username=username,
            display_name=display_name,
            home_instance_pk=home_pk,
            picture_url=str(picture_url) if picture_url else None,
        )
        return web.json_response(
            {
                "user_id": reg.user_id,
                "registered_at": reg.registered_at,
                "status": reg.status,
            },
            status=201,
        )


class GfsUserDeregisterView(GfsBaseView):
    """``POST /gfs/users/{user_id}/deregister`` — pull a registration."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        _, sender_instance_id = result
        registry = self.svc(K.gfs_moment_public_registry_key)
        user_id = self.match("user_id")
        existing = await registry.get_registration(user_id)
        if existing is None:
            return web.json_response({"deregistered": False}, status=404)
        if existing.instance_id != sender_instance_id:
            return web.json_response({"error": "instance_mismatch"}, status=403)
        ok = await registry.deregister_user(user_id)
        return web.json_response({"deregistered": ok})


class GfsUserFollowView(GfsBaseView):
    """``POST /gfs/users/{user_id}/follow`` — record a follower."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, sender_instance_id = result
        registry = self.svc(K.gfs_moment_public_registry_key)
        followed_user_id = self.match("user_id")
        follower_user_id = str(body.get("follower_user_id") or "")
        follower_instance_id = str(body.get("follower_instance_id") or "")
        if follower_instance_id != sender_instance_id:
            return web.json_response({"error": "instance_mismatch"}, status=403)
        if not follower_user_id:
            return web.json_response({"error": "follower_user_id_required"}, status=422)
        try:
            follow = await registry.add_follow(
                follower_user_id=follower_user_id,
                follower_instance_id=follower_instance_id,
                followed_user_id=followed_user_id,
            )
        except LookupError:
            return web.json_response({"error": "author_not_found"}, status=404)
        # Surface the followed user's directory entry in the response so
        # the follower's instance can cache the home_instance_pk it
        # needs for signature verification on every subsequent moment.
        target = await registry.get_registration(followed_user_id)
        if target is None:  # pragma: no cover — race with deregister
            return web.json_response({"error": "author_not_found"}, status=404)
        return web.json_response(
            {
                "user": {
                    "user_id": target.user_id,
                    "username": target.username,
                    "display_name": target.display_name,
                    "picture_url": target.picture_url,
                    "home_instance_pk": target.home_instance_pk,
                    "instance_id": target.instance_id,
                },
                "follow": {
                    "follower_user_id": follow.follower_user_id,
                    "followed_user_id": follow.followed_user_id,
                    "created_at": follow.created_at,
                },
            },
            status=201,
        )


class GfsUserUnfollowView(GfsBaseView):
    """``POST /gfs/users/{user_id}/unfollow`` — drop a follower."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, sender_instance_id = result
        registry = self.svc(K.gfs_moment_public_registry_key)
        followed_user_id = self.match("user_id")
        follower_user_id = str(body.get("follower_user_id") or "")
        follower_instance_id = str(body.get("follower_instance_id") or "")
        if follower_instance_id != sender_instance_id:
            return web.json_response({"error": "instance_mismatch"}, status=403)
        ok = await registry.remove_follow(
            follower_user_id=follower_user_id,
            followed_user_id=followed_user_id,
        )
        return web.json_response({"unfollowed": ok})


class GfsMomentPublicPublishView(GfsBaseView):
    """``POST /gfs/moments/publish`` — fan a signed moment to followers."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, _sender = result
        registry = self.svc(K.gfs_moment_public_registry_key)
        delivered = await registry.fan_out_moment(envelope=body)
        return web.json_response({"delivered": delivered})


class GfsMomentPublicDeleteView(GfsBaseView):
    """``POST /gfs/moments/delete`` — fan a tombstone."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, _sender = result
        registry = self.svc(K.gfs_moment_public_registry_key)
        delivered = await registry.fan_out_delete(envelope=body)
        return web.json_response({"delivered": delivered})


# ─── Public discovery ──────────────────────────────────────────────────────


class GfsUserDirectoryView(GfsBaseView):
    """``GET /gfs/users`` — JSON listing of active registrations."""

    async def get(self) -> web.Response:
        registry = self.svc(K.gfs_moment_public_registry_key)
        regs = await registry.list_directory()
        return web.json_response(
            {
                "users": [
                    {
                        "user_id": r.user_id,
                        "instance_id": r.instance_id,
                        "username": r.username,
                        "display_name": r.display_name,
                        "picture_url": r.picture_url,
                        "home_instance_pk": r.home_instance_pk,
                        "registered_at": r.registered_at,
                    }
                    for r in regs
                ]
            }
        )


class GfsUserDirectoryHtmlView(GfsBaseView):
    """``GET /users`` — lightweight rendered directory page."""

    async def get(self) -> web.Response:
        registry = self.svc(K.gfs_moment_public_registry_key)
        regs = await registry.list_directory()
        rows = "".join(
            f"<li><strong>{_html_escape(r.display_name)}</strong> "
            f"<small>@{_html_escape(r.username)} · {_html_escape(r.instance_id)}</small></li>"
            for r in regs
        )
        body = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Public Momentum directory</title></head><body>"
            f"<h1>Public Momentum users ({len(regs)})</h1><ul>{rows or '<li>No registered users yet.</li>'}</ul>"
            "</body></html>"
        )
        return web.Response(text=body, content_type="text/html")


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
