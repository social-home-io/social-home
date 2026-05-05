"""Moments routes — ``/api/moments/*`` (§Momentum).

REST surface for the household-broadcast Moments pillar. Every
endpoint here is auth-gated and household-feature-gated by
``feat_momentum``. The service layer (``services/moment_service.py``)
does the orchestration; this file translates HTTP to method calls and
JSON.

Routes:

* ``POST   /api/moments``                     — create a moment (text + media + parent_moment_id?)
* ``GET    /api/moments``                     — list visible moments (24h default; 7d for followers)
* ``GET    /api/moments/archive``             — full retention-window list (same data, full window)
* ``GET    /api/moments/{id}``                — detail incl. replies + reactions
* ``DELETE /api/moments/{id}``                — author / admin delete
* ``PUT    /api/moments/{id}/reaction``       — set / change reaction
* ``DELETE /api/moments/{id}/reaction``       — clear own reaction
* ``POST   /api/moments/{id}/report``         — wrap content_reports
* ``GET    /api/moments/follows``             — list of who I follow
* ``POST   /api/moments/follows``             — follow a user
* ``DELETE /api/moments/follows/{user_id}``   — unfollow
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import (
    moment_service_key,
    report_service_key,
    user_service_key,
)
from ..domain.moment import Moment, MomentReaction
from ..domain.report import (
    DuplicateReportError,
    ReportRateLimitedError,
    ReportTargetType,
)
from ..security import error_response
from .base import BaseView


def _moment_dict(m: Moment) -> dict:
    return {
        "id": m.id,
        "author_user_id": m.author_user_id,
        "content": m.content,
        "media_url": m.media_url,
        "media_type": m.media_type,
        "duration_ms": m.duration_ms,
        "parent_moment_id": m.parent_moment_id,
        "origin_instance_id": m.origin_instance_id,
        "created_at": m.created_at,
        "expires_at": m.expires_at,
    }


def _reaction_dict(r: MomentReaction) -> dict:
    return {
        "moment_id": r.moment_id,
        "reactor_user_id": r.reactor_user_id,
        "emoji": r.emoji,
        "reacted_at": r.reacted_at,
    }


# ─── Moments collection / detail ─────────────────────────────────────────


class MomentCollectionView(BaseView):
    """``GET|POST /api/moments``."""

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        await self.require_household_feature("momentum")
        svc = self.svc(moment_service_key)
        before = self.request.query.get("before")
        try:
            limit = int(self.request.query.get("limit", 50))
        except ValueError:
            limit = 50
        moments = await svc.list_inbox(
            viewer_user_id=ctx.user_id,
            before=before,
            limit=limit,
        )
        return self._json([_moment_dict(m) for m in moments])

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        await self.require_household_feature("momentum")
        body = await self.body()
        svc = self.svc(moment_service_key)
        moment = await svc.create_moment(
            author_user_id=ctx.user_id,
            content=str(body.get("content") or ""),
            media_url=body.get("media_url"),
            media_type=body.get("media_type"),
            duration_ms=body.get("duration_ms"),
            parent_moment_id=body.get("parent_moment_id"),
        )
        return self._json(_moment_dict(moment), status=201)


class MomentArchiveView(BaseView):
    """``GET /api/moments/archive`` — full retention-window list.

    Same data shape as the inbox; the page chrome on the SPA side
    differs (calendar grouping vs. flat list). The visibility filter
    (block-aware, follower-aware) still applies.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        await self.require_household_feature("momentum")
        svc = self.svc(moment_service_key)
        moments = await svc.list_inbox(viewer_user_id=ctx.user_id, limit=100)
        return self._json([_moment_dict(m) for m in moments])


class MomentDetailView(BaseView):
    """``GET|DELETE /api/moments/{id}``."""

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        await self.require_household_feature("momentum")
        svc = self.svc(moment_service_key)
        moment_id = self.match("id")
        moment = await svc.get_moment(moment_id)
        replies = await svc.list_replies(moment_id)
        reactions = await svc.list_reactions(moment_id)
        return self._json(
            {
                "moment": _moment_dict(moment),
                "replies": [_moment_dict(r) for r in replies],
                "reactions": [_reaction_dict(r) for r in reactions],
            }
        )

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        await self.require_household_feature("momentum")
        svc = self.svc(moment_service_key)
        moment_id = self.match("id")
        await svc.delete_moment(
            moment_id,
            actor_user_id=ctx.user_id,
            actor_is_admin=ctx.is_admin,
        )
        return self._json({"id": moment_id, "deleted": True})


class MomentReactionView(BaseView):
    """``PUT|DELETE /api/moments/{id}/reaction``."""

    async def put(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        await self.require_household_feature("momentum")
        body = await self.body()
        emoji = str(body.get("emoji") or "").strip()
        if not emoji:
            return error_response(422, "UNPROCESSABLE", "emoji is required.")
        svc = self.svc(moment_service_key)
        moment_id = self.match("id")
        await svc.react(moment_id, reactor_user_id=ctx.user_id, emoji=emoji)
        return self._json({"moment_id": moment_id, "emoji": emoji})

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        await self.require_household_feature("momentum")
        svc = self.svc(moment_service_key)
        moment_id = self.match("id")
        await svc.clear_reaction(moment_id, reactor_user_id=ctx.user_id)
        return self._json({"moment_id": moment_id, "emoji": None})


class MomentReportView(BaseView):
    """``POST /api/moments/{id}/report`` — wraps the existing
    :class:`ReportService` with ``target_type='moment'`` pre-filled."""

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        await self.require_household_feature("momentum")
        body = await self.body()
        category = str(body.get("category") or "").strip()
        if not category:
            return error_response(422, "UNPROCESSABLE", "category is required.")
        notes = body.get("notes")
        svc = self.svc(report_service_key)
        moment_id = self.match("id")
        try:
            report, federated = await svc.create_report(
                reporter_user_id=ctx.user_id,
                target_type=ReportTargetType.MOMENT.value,
                target_id=moment_id,
                category=category,
                notes=str(notes) if notes else None,
                forward_gfs=False,
            )
        except DuplicateReportError as exc:
            return error_response(409, "DUPLICATE_REPORT", str(exc))
        except ReportRateLimitedError as exc:
            return error_response(429, "REPORT_RATE_LIMIT", str(exc))
        return self._json(
            {
                "id": report.id,
                "status": report.status.value,
                "federated": federated,
            },
            status=201,
        )


# ─── Follows ──────────────────────────────────────────────────────────────


class MomentFollowsCollectionView(BaseView):
    """``GET|POST /api/moments/follows``.

    Follows live on the same surface as moments because they only
    affect the Momentum retention window today (24h → 7d when the
    viewer follows). Future surfaces can also consult ``user_follows``
    without changing the route shape.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        svc = self.svc(user_service_key)
        rows = await svc.list_following(ctx.username)
        return self._json({"follows": rows})

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        body = await self.body()
        target = str(body.get("user_id") or "").strip()
        if not target:
            return error_response(400, "BAD_REQUEST", "Missing user_id.")
        svc = self.svc(user_service_key)
        await svc.follow(ctx.username, target)
        return self._json({"user_id": target}, status=201)


class MomentFollowsDetailView(BaseView):
    """``DELETE /api/moments/follows/{user_id}``."""

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        target = self.match("user_id")
        svc = self.svc(user_service_key)
        await svc.unfollow(ctx.username, target)
        return self._json({"user_id": target})
