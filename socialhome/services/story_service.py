"""Story service (§Stories).

Orchestrates the create/append/delete/share/expire flows on top of
:class:`AbstractStoryRepo`. Holds no SQL, no HTTP, no media validation —
those live in repos, routes, and the existing ``MediaValidator``
respectively.

The encryption-first rule (§25.8.21) shapes the federation outbound:
this service collects the *plaintext* domain event (id, sequence,
audience, expires_at, captions, media url) and emits a
:class:`StoryFrameAdded` (or sibling) on the bus. The
``StoryFederationOutbound`` subscriber receives it, fans out to peer
instances based on the audience kind, and each per-peer envelope sends
the body inside the encrypted payload. See
``story_federation_outbound.py``.

Authorization: callers are responsible for verifying the
``actor_user_id`` matches the resource owner (route layer does this via
:class:`BaseView`). The service still raises :class:`PermissionError`
when an actor tries to mutate someone else's frame / story so any
mis-wired caller can't do silent damage.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..domain.events import (
    StoryFrameAdded,
    StoryFrameReactionChanged,
    StoryFrameRemoved,
    StoryFrameViewed,
    StoryRemoved,
)
from ..domain.story import (
    Story,
    StoryAudience,
    StoryFrame,
    StoryFrameReplySnapshot,
    StoryFrameType,
)
from .user_preferences import parse_stories_preferences

if TYPE_CHECKING:
    from ..infrastructure.event_bus import EventBus
    from ..repositories.story_repo import AbstractStoryRepo
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)

#: Hard ceiling on frames per story per day. Prevents storage abuse and
#: keeps the viewer responsive on slow networks.
MAX_FRAMES_PER_STORY: int = 30


class StoryNotFoundError(LookupError):
    """The targeted story or frame does not exist."""


class StoryForbiddenError(PermissionError):
    """The actor is not allowed to mutate this story / frame."""


class StoryFrameLimitError(ValueError):
    """The author's per-day frame cap has been reached."""


class StoryService:
    """Personal stories: create / view / react / share / expire."""

    __slots__ = ("_stories", "_users", "_bus")

    def __init__(
        self,
        repo: "AbstractStoryRepo",
        user_repo: "AbstractUserRepo",
        bus: "EventBus",
    ) -> None:
        self._stories = repo
        self._users = user_repo
        self._bus = bus

    # ── Create / append ─────────────────────────────────────────────────

    async def create_or_append_frame(
        self,
        *,
        author_user_id: str,
        frame_type: StoryFrameType | str,
        media_url: str,
        caption_text: str | None = None,
        caption_emoji: str | None = None,
        duration_ms: int | None = None,
        audience_kind: StoryAudience | str | None = None,
        audience: tuple[str, ...] | list[str] | None = None,
    ) -> tuple[Story, StoryFrame]:
        """Find or create today's story for *author_user_id* and append a frame.

        Returns ``(story, frame)``. Publishes :class:`StoryFrameAdded`
        with ``is_first_frame=True`` for the very first frame of the day
        (so federation outbound can pick the right
        :class:`FederationEventType`). Subsequent frames the same day
        re-use the existing :class:`Story` row.
        """
        author = await self._users.get_by_user_id(author_user_id)
        if author is None:
            raise LookupError(f"author {author_user_id!r} not found")
        prefs = parse_stories_preferences(author.preferences_json)

        # Default audience flows from the author's preferences when the
        # caller did not pin one explicitly.
        if audience_kind is None:
            kind = prefs.default_audience_kind
            ids: tuple[str, ...] = prefs.default_audience
        else:
            kind = (
                audience_kind
                if isinstance(audience_kind, StoryAudience)
                else StoryAudience(audience_kind)
            )
            ids = tuple(audience or ())
            if kind is StoryAudience.ALL_PAIRED:
                ids = ()

        ftype = (
            frame_type
            if isinstance(frame_type, StoryFrameType)
            else StoryFrameType(frame_type)
        )

        now = datetime.now(timezone.utc)
        story_date = now.strftime("%Y-%m-%d")
        expires_at = (now + timedelta(days=prefs.retention_days)).isoformat()

        story = await self._stories.find_or_create_today(
            author_user_id=author_user_id,
            audience_kind=kind,
            audience=ids,
            story_date=story_date,
            expires_at=expires_at,
        )

        # Cap frames per story per day.
        existing = await self._stories.list_frames(story.id)
        is_first = len(existing) == 0
        if len(existing) >= MAX_FRAMES_PER_STORY:
            raise StoryFrameLimitError(
                f"story already has {MAX_FRAMES_PER_STORY} frames today"
            )

        frame = await self._stories.append_frame(
            story_id=story.id,
            frame_type=ftype,
            media_url=media_url,
            caption_text=caption_text,
            caption_emoji=caption_emoji,
            duration_ms=duration_ms,
        )

        await self._bus.publish(
            StoryFrameAdded(
                story_id=story.id,
                frame_id=frame.id,
                author_user_id=author_user_id,
                story_date=story.story_date,
                sequence=frame.sequence,
                is_first_frame=is_first,
                audience_kind=story.audience_kind.value,
                audience=story.audience,
                frame_type=ftype.value,
                media_url=media_url,
                caption_text=caption_text,
                caption_emoji=caption_emoji,
                duration_ms=duration_ms,
                expires_at=story.expires_at or expires_at,
            )
        )
        return story, frame

    # ── View / react ───────────────────────────────────────────────────

    async def mark_frame_viewed(
        self,
        *,
        frame_id: str,
        viewer_user_id: str,
    ) -> None:
        frame = await self._stories.get_frame(frame_id)
        if frame is None:
            raise StoryNotFoundError(frame_id)
        story = await self._stories.get_story(frame.story_id)
        if story is None:
            raise StoryNotFoundError(frame.story_id)
        if story.author_user_id == viewer_user_id:
            # Authors don't accumulate views on their own frames.
            return
        await self._stories.mark_viewed(frame_id, viewer_user_id)
        await self._bus.publish(
            StoryFrameViewed(
                story_id=story.id,
                frame_id=frame.id,
                viewer_user_id=viewer_user_id,
                author_user_id=story.author_user_id,
            )
        )

    async def react_to_frame(
        self,
        *,
        frame_id: str,
        reactor_user_id: str,
        emoji: str,
    ) -> None:
        frame = await self._stories.get_frame(frame_id)
        if frame is None:
            raise StoryNotFoundError(frame_id)
        story = await self._stories.get_story(frame.story_id)
        if story is None:
            raise StoryNotFoundError(frame.story_id)
        await self._stories.set_reaction(frame_id, reactor_user_id, emoji)
        await self._bus.publish(
            StoryFrameReactionChanged(
                story_id=story.id,
                frame_id=frame.id,
                reactor_user_id=reactor_user_id,
                author_user_id=story.author_user_id,
                emoji=emoji,
            )
        )

    async def clear_reaction(
        self,
        *,
        frame_id: str,
        reactor_user_id: str,
    ) -> None:
        frame = await self._stories.get_frame(frame_id)
        if frame is None:
            raise StoryNotFoundError(frame_id)
        story = await self._stories.get_story(frame.story_id)
        if story is None:
            raise StoryNotFoundError(frame.story_id)
        await self._stories.clear_reaction(frame_id, reactor_user_id)
        await self._bus.publish(
            StoryFrameReactionChanged(
                story_id=story.id,
                frame_id=frame.id,
                reactor_user_id=reactor_user_id,
                author_user_id=story.author_user_id,
                emoji=None,
            )
        )

    # ── Delete ─────────────────────────────────────────────────────────

    async def delete_frame(
        self,
        *,
        frame_id: str,
        actor_user_id: str,
    ) -> None:
        frame = await self._stories.get_frame(frame_id)
        if frame is None:
            raise StoryNotFoundError(frame_id)
        story = await self._stories.get_story(frame.story_id)
        if story is None:
            raise StoryNotFoundError(frame.story_id)
        if story.author_user_id != actor_user_id:
            raise StoryForbiddenError("only the author can delete their frame")
        await self._stories.delete_frame(frame_id)
        await self._bus.publish(
            StoryFrameRemoved(
                story_id=story.id,
                frame_id=frame_id,
                author_user_id=story.author_user_id,
                audience_kind=story.audience_kind.value,
                audience=story.audience,
            )
        )

    async def delete_story(
        self,
        *,
        story_id: str,
        actor_user_id: str,
    ) -> None:
        story = await self._stories.get_story(story_id)
        if story is None:
            raise StoryNotFoundError(story_id)
        if story.author_user_id != actor_user_id:
            raise StoryForbiddenError("only the author can delete their story")
        await self._stories.delete_story(story_id)
        await self._bus.publish(
            StoryRemoved(
                story_id=story.id,
                author_user_id=story.author_user_id,
                audience_kind=story.audience_kind.value,
                audience=story.audience,
            )
        )

    # ── Read ───────────────────────────────────────────────────────────

    async def list_visible(self, viewer_user_id: str) -> list[dict[str, Any]]:
        """Return the inbox view: stories visible to *viewer_user_id*.

        Each entry is a dict ready for the route layer to serialise:
        ``{story, frames, unseen_count}`` — frames are
        :class:`StoryFrame` instances, ``unseen_count`` is an ``int``.
        Sorted newest-first by ``story_date``.
        """
        stories = await self._stories.list_visible_to(viewer_user_id)
        out: list[dict[str, Any]] = []
        for story in stories:
            frames = await self._stories.list_frames(story.id)
            unseen = await self._stories.count_unseen_frames(story.id, viewer_user_id)
            out.append(
                {
                    "story": story,
                    "frames": frames,
                    "unseen_count": unseen,
                }
            )
        return out

    async def get_with_frames(
        self,
        story_id: str,
    ) -> tuple[Story, list[StoryFrame]] | None:
        story = await self._stories.get_story(story_id)
        if story is None:
            return None
        frames = await self._stories.list_frames(story_id)
        return story, frames

    async def get_frame(self, frame_id: str) -> StoryFrame | None:
        return await self._stories.get_frame(frame_id)

    # ── Share into a feed ──────────────────────────────────────────────

    async def share_to_feed(
        self,
        *,
        story_id: str,
        actor_user_id: str,
        scope: str,  # 'household' | 'space'
        space_id: str | None,
        note: str | None,
        feed_service: Any,
        space_service: Any,
    ) -> Any:
        """Create a ``story_share`` post in the household or space feed.

        The route layer threads the existing feed/space services in via
        the keyword arguments — keeps :class:`StoryService` from holding
        a hard dependency on either service module while still avoiding
        SQL in the service.
        """
        story = await self._stories.get_story(story_id)
        if story is None:
            raise StoryNotFoundError(story_id)
        # Only the author can share their own story (avoids re-sharing
        # someone else's content outside of the audience the author
        # picked).
        if story.author_user_id != actor_user_id:
            raise StoryForbiddenError(
                "only the author of a story can share it into a feed"
            )
        body = (note or "").strip() or None
        if scope == "household":
            return await feed_service.create_post(
                author_user_id=actor_user_id,
                type="story_share",
                content=body,
                linked_story_id=story.id,
            )
        if scope == "space":
            if not space_id:
                raise ValueError("space_id is required when scope='space'")
            return await space_service.create_post(
                space_id=space_id,
                author_user_id=actor_user_id,
                type="story_share",
                content=body,
                linked_story_id=story.id,
            )
        raise ValueError(f"unknown share scope: {scope!r}")

    # ── DM reply to a frame ────────────────────────────────────────────

    async def dm_reply_to_frame(
        self,
        *,
        frame_id: str,
        sender_user_id: str,
        conversation_id: str,
        content: str,
        dm_service: Any,
    ) -> Any:
        """Send a DM that quotes a story frame.

        Reuses the existing DM send pipeline; the snapshot is frozen on
        the message (as a JSON blob) so the reply stays meaningful
        after retention purges the underlying frame.
        """
        frame = await self._stories.get_frame(frame_id)
        if frame is None:
            raise StoryNotFoundError(frame_id)
        story = await self._stories.get_story(frame.story_id)
        if story is None:
            raise StoryNotFoundError(frame.story_id)
        sender = await self._users.get_by_user_id(sender_user_id)
        if sender is None:
            raise LookupError(f"sender {sender_user_id!r} not found")
        snapshot = StoryFrameReplySnapshot(
            thumb_url=frame.media_url,
            author_user_id=story.author_user_id,
            story_date=story.story_date,
            caption_text=frame.caption_text,
            caption_emoji=frame.caption_emoji,
        )
        snapshot_json = json.dumps(
            {
                "thumb_url": snapshot.thumb_url,
                "author_user_id": snapshot.author_user_id,
                "story_date": snapshot.story_date,
                "caption_text": snapshot.caption_text,
                "caption_emoji": snapshot.caption_emoji,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return await dm_service.send_message(
            conversation_id,
            sender_username=sender.username,
            content=content,
            reply_to_story_frame_id=frame.id,
            reply_to_story_frame_snapshot=snapshot_json,
        )

    # ── Retention ──────────────────────────────────────────────────────

    async def expire_due(self) -> tuple[int, int]:
        """Run one retention pass.

        Two phases:
          1. ``prune_expired`` — drop stories past their author-set
             retention cutoff.
          2. ``prune_over_max`` per author — once the author has more
             than their ``max_count`` stories the oldest are dropped.

        Returns ``(expired_count, over_max_count)`` for telemetry. Safe
        to call as often as the scheduler ticks; the underlying queries
        are idempotent.
        """
        expired = await self._stories.prune_expired()
        over_max = 0
        authors = await self._stories.list_authors_with_stories()
        for author_user_id in authors:
            user = await self._users.get_by_user_id(author_user_id)
            prefs = parse_stories_preferences(
                user.preferences_json if user is not None else None
            )
            over_max += await self._stories.prune_over_max(
                author_user_id, prefs.max_count
            )
        return expired, over_max


__all__ = [
    "MAX_FRAMES_PER_STORY",
    "StoryForbiddenError",
    "StoryFrameLimitError",
    "StoryNotFoundError",
    "StoryService",
]


# Used by tests / CLI to mint deterministic ids when needed; main code
# uses uuid4 inside the repos.
def _new_id() -> str:
    return uuid.uuid4().hex
