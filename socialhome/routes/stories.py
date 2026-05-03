"""Stories routes — ``/api/stories/*`` (§Stories).

REST surface for the personal stories pillar. Every endpoint here is
auth-gated and household-feature-gated by ``feat_stories`` (§22). The
service layer (``services/story_service.py``) does the orchestration —
this file only translates HTTP to method calls and the result back to
JSON.

Routes:

* ``POST   /api/stories/frames``                       — append today's frame
* ``GET    /api/stories``                              — list visible stories
* ``GET    /api/stories/{id}``                         — story detail
* ``DELETE /api/stories/{id}``                         — delete whole story
* ``DELETE /api/stories/frames/{id}``                  — delete one frame
* ``POST   /api/stories/frames/{id}/view``             — mark viewed
* ``PUT    /api/stories/frames/{id}/reaction``         — set / change reaction
* ``DELETE /api/stories/frames/{id}/reaction``         — clear reaction
* ``POST   /api/stories/{id}/share``                   — share into a feed
* ``POST   /api/stories/frames/{id}/dm-reply``         — DM reply with snapshot

The DM-reply endpoint is intentionally on the stories surface (rather
than ``/api/conversations/.../messages``) so the snapshot construction
stays next to the story domain. Internally it routes through
``DmService`` which carries the standard DM federation + WS path.
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import (
    dm_service_key,
    feed_service_key,
    space_service_key,
    story_service_key,
)
from ..domain.story import (
    Story,
    StoryAudience,
    StoryFrame,
    StoryFrameType,
)
from ..security import error_response
from .base import BaseView


# ─── Serialisation helpers ────────────────────────────────────────────────


def _story_dict(story: Story) -> dict:
    return {
        "id": story.id,
        "author_user_id": story.author_user_id,
        "story_date": story.story_date,
        "audience_kind": story.audience_kind.value,
        "audience": list(story.audience),
        "created_at": story.created_at,
        "expires_at": story.expires_at,
    }


def _frame_dict(frame: StoryFrame) -> dict:
    return {
        "id": frame.id,
        "story_id": frame.story_id,
        "sequence": frame.sequence,
        "frame_type": frame.frame_type.value,
        "media_url": frame.media_url,
        "caption_text": frame.caption_text,
        "caption_emoji": frame.caption_emoji,
        "duration_ms": frame.duration_ms,
        "created_at": frame.created_at,
    }


# ─── Views ────────────────────────────────────────────────────────────────


class StoryFramesCollectionView(BaseView):
    """``POST /api/stories/frames`` — create or append today's frame."""

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        await self.require_household_feature("stories")
        body = await self.body()
        media_url = body.get("media_url")
        if not isinstance(media_url, str) or not media_url:
            return error_response(400, "BAD_REQUEST", "media_url is required")
        frame_type_raw = body.get("frame_type", "image")
        try:
            frame_type = StoryFrameType(frame_type_raw)
        except ValueError:
            return error_response(
                400, "BAD_REQUEST", f"invalid frame_type {frame_type_raw!r}"
            )
        audience_kind_raw = body.get("audience_kind")
        audience_kind: StoryAudience | None = None
        if audience_kind_raw is not None:
            try:
                audience_kind = StoryAudience(audience_kind_raw)
            except ValueError:
                return error_response(
                    400,
                    "BAD_REQUEST",
                    f"invalid audience_kind {audience_kind_raw!r}",
                )
        audience_ids_raw = body.get("audience") or []
        if not isinstance(audience_ids_raw, list):
            return error_response(400, "BAD_REQUEST", "audience must be a list")
        audience_ids: tuple[str, ...] = tuple(
            str(x) for x in audience_ids_raw if isinstance(x, str)
        )

        svc = self.svc(story_service_key)
        story, frame = await svc.create_or_append_frame(
            author_user_id=ctx.user_id,
            frame_type=frame_type,
            media_url=media_url,
            caption_text=body.get("caption_text"),
            caption_emoji=body.get("caption_emoji"),
            duration_ms=body.get("duration_ms"),
            audience_kind=audience_kind,
            audience=audience_ids,
        )
        return self._json(
            {"story": _story_dict(story), "frame": _frame_dict(frame)},
            status=201,
        )


class StoriesCollectionView(BaseView):
    """``GET /api/stories`` — list stories visible to the caller."""

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        await self.require_household_feature("stories")
        svc = self.svc(story_service_key)
        rows = await svc.list_visible(ctx.user_id)
        out: list[dict] = []
        for row in rows:
            out.append(
                {
                    "story": _story_dict(row["story"]),
                    "frames": [_frame_dict(f) for f in row["frames"]],
                    "unseen_count": row["unseen_count"],
                }
            )
        return self._json(out)


class StoryDetailView(BaseView):
    """``GET /api/stories/{id}`` + ``DELETE /api/stories/{id}``."""

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        story_id = self.match("id")
        svc = self.svc(story_service_key)
        result = await svc.get_with_frames(story_id)
        if result is None:
            return error_response(404, "NOT_FOUND", "story not found")
        story, frames = result
        body = {
            "story": _story_dict(story),
            "frames": [_frame_dict(f) for f in frames],
        }
        # Authors get the per-frame views + reactions inline so the
        # viewer can render "Viewed by N" without a second request.
        if story.author_user_id == ctx.user_id:
            views_by_frame: dict[str, list[dict]] = {}
            reactions_by_frame: dict[str, list[dict]] = {}
            repo = self.svc(story_service_key)._stories
            for f in frames:
                vs = await repo.list_views_for_frame(f.id)
                rs = await repo.list_reactions_for_frame(f.id)
                views_by_frame[f.id] = [
                    {"viewer_user_id": v.viewer_user_id, "viewed_at": v.viewed_at}
                    for v in vs
                ]
                reactions_by_frame[f.id] = [
                    {
                        "reactor_user_id": r.reactor_user_id,
                        "emoji": r.emoji,
                        "reacted_at": r.reacted_at,
                    }
                    for r in rs
                ]
            body["views"] = views_by_frame
            body["reactions"] = reactions_by_frame
        return self._json(body)

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        story_id = self.match("id")
        svc = self.svc(story_service_key)
        await svc.delete_story(story_id=story_id, actor_user_id=ctx.user_id)
        return web.json_response({}, status=204)


class StoryFrameDetailView(BaseView):
    """``DELETE /api/stories/frames/{id}`` — author removes a frame."""

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        frame_id = self.match("id")
        svc = self.svc(story_service_key)
        await svc.delete_frame(frame_id=frame_id, actor_user_id=ctx.user_id)
        return web.json_response({}, status=204)


class StoryFrameViewView(BaseView):
    """``POST /api/stories/frames/{id}/view`` — mark a frame seen."""

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        frame_id = self.match("id")
        svc = self.svc(story_service_key)
        await svc.mark_frame_viewed(frame_id=frame_id, viewer_user_id=ctx.user_id)
        return web.json_response({"ok": True})


class StoryFrameReactionView(BaseView):
    """``PUT/DELETE /api/stories/frames/{id}/reaction``."""

    async def put(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        frame_id = self.match("id")
        body = await self.body()
        emoji = body.get("emoji")
        if not isinstance(emoji, str) or not emoji.strip():
            return error_response(400, "BAD_REQUEST", "emoji is required")
        svc = self.svc(story_service_key)
        await svc.react_to_frame(
            frame_id=frame_id,
            reactor_user_id=ctx.user_id,
            emoji=emoji.strip(),
        )
        return web.json_response({"ok": True})

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        frame_id = self.match("id")
        svc = self.svc(story_service_key)
        await svc.clear_reaction(frame_id=frame_id, reactor_user_id=ctx.user_id)
        return web.json_response({"ok": True})


class StoryShareView(BaseView):
    """``POST /api/stories/{id}/share`` — share into a feed."""

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        story_id = self.match("id")
        body = await self.body()
        scope = body.get("scope")
        if scope not in ("household", "space"):
            return error_response(
                400, "BAD_REQUEST", "scope must be 'household' or 'space'"
            )
        space_id = body.get("space_id")
        note = body.get("note")
        svc = self.svc(story_service_key)
        post = await svc.share_to_feed(
            story_id=story_id,
            actor_user_id=ctx.user_id,
            scope=scope,
            space_id=space_id,
            note=note,
            feed_service=self.svc(feed_service_key),
            space_service=self.svc(space_service_key),
        )
        # ``post`` may be ``None`` for moderated space scopes — surface a
        # 202 Accepted so the SPA shows a "queued for review" toast.
        if post is None:
            return web.json_response({"queued": True}, status=202)
        return self._json(
            {"post_id": getattr(post, "id", None), "story_id": story_id},
            status=201,
        )


class StoryDmReplyView(BaseView):
    """``POST /api/stories/frames/{id}/dm-reply`` — DM with frame snapshot."""

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "auth required")
        frame_id = self.match("id")
        body = await self.body()
        conversation_id = body.get("conversation_id")
        content = body.get("content", "")
        if not isinstance(conversation_id, str) or not conversation_id:
            return error_response(400, "BAD_REQUEST", "conversation_id is required")
        if not isinstance(content, str):
            return error_response(400, "BAD_REQUEST", "content must be a string")
        svc = self.svc(story_service_key)
        message = await svc.dm_reply_to_frame(
            frame_id=frame_id,
            sender_user_id=ctx.user_id,
            conversation_id=conversation_id,
            content=content,
            dm_service=self.svc(dm_service_key),
        )
        return self._json(
            {"message_id": getattr(message, "id", None)},
            status=201,
        )


__all__ = [
    "StoriesCollectionView",
    "StoryDetailView",
    "StoryDmReplyView",
    "StoryFrameDetailView",
    "StoryFrameReactionView",
    "StoryFrameViewView",
    "StoryFramesCollectionView",
    "StoryShareView",
]
