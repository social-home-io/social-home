"""Service-level tests for the stories pillar."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.events import (
    StoryFrameAdded,
    StoryFrameReactionChanged,
    StoryFrameViewed,
)
from socialhome.domain.story import StoryAudience, StoryFrameType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.story_repo import SqliteStoryRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.story_service import (
    MAX_FRAMES_PER_STORY,
    StoryForbiddenError,
    StoryFrameLimitError,
    StoryNotFoundError,
    StoryService,
)


async def _seed_user(
    db, user_id: str, username: str, *, prefs_json: str | None = None
) -> None:
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state, preferences_json) "
        "VALUES(?, ?, ?, 'active', COALESCE(?, '{}'))",
        (user_id, username, username.title(), prefs_json),
    )


@pytest.fixture
async def svc(db):
    bus = EventBus()
    return StoryService(SqliteStoryRepo(db), SqliteUserRepo(db), bus), bus


async def test_first_frame_creates_story_and_publishes(db, svc):
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    captured: list[StoryFrameAdded] = []
    bus.subscribe(StoryFrameAdded, lambda e: captured.append(e))

    story, frame = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/a.webp",
        caption_text="hello",
    )
    assert story.author_user_id == "u1"
    assert frame.sequence == 1
    assert story.audience_kind is StoryAudience.ALL_PAIRED
    assert len(captured) == 1
    assert captured[0].is_first_frame is True


async def test_second_frame_appends_to_same_story(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    s1, f1 = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/a.webp",
    )
    s2, f2 = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/b.webp",
    )
    assert s1.id == s2.id
    assert f1.sequence == 1 and f2.sequence == 2


async def test_frame_cap_raises(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    for _ in range(MAX_FRAMES_PER_STORY):
        await service.create_or_append_frame(
            author_user_id="u1",
            frame_type=StoryFrameType.IMAGE,
            media_url="/api/media/x.webp",
        )
    with pytest.raises(StoryFrameLimitError):
        await service.create_or_append_frame(
            author_user_id="u1",
            frame_type=StoryFrameType.IMAGE,
            media_url="/api/media/y.webp",
        )


async def test_view_publishes_event(db, svc):
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    _, frame = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    captured: list[StoryFrameViewed] = []
    bus.subscribe(StoryFrameViewed, lambda e: captured.append(e))
    await service.mark_frame_viewed(frame_id=frame.id, viewer_user_id="u2")
    assert len(captured) == 1
    assert captured[0].viewer_user_id == "u2"


async def test_author_view_is_noop(db, svc):
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    _, frame = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    captured: list[StoryFrameViewed] = []
    bus.subscribe(StoryFrameViewed, lambda e: captured.append(e))
    await service.mark_frame_viewed(frame_id=frame.id, viewer_user_id="u1")
    assert captured == []  # author's own views don't accumulate


async def test_retention_uses_author_preferences(db, svc):
    service, _ = svc
    await _seed_user(
        db,
        "u1",
        "pascal",
        prefs_json='{"stories": {"retention_days": 1}}',
    )
    story, _ = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/a.webp",
    )
    # Author's retention is 1 day so expires_at is ~24h out — much less
    # than the 30-day default.
    assert story.expires_at is not None
    expires = datetime.fromisoformat(story.expires_at)
    cutoff_30d = datetime.now(timezone.utc) + timedelta(days=29)
    assert expires < cutoff_30d


async def test_react_then_clear(db, svc):
    """Reaction set + clear publish events; clear removes the row."""
    service, bus = svc
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    _, frame = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    cleared = []
    bus.subscribe(StoryFrameReactionChanged, lambda e: cleared.append(e))
    await service.react_to_frame(
        frame_id=frame.id,
        reactor_user_id="u2",
        emoji="🔥",
    )
    await service.clear_reaction(frame_id=frame.id, reactor_user_id="u2")
    # Two events: set then clear (emoji=None on the second).
    assert len(cleared) == 2
    assert cleared[1].emoji is None


async def test_delete_frame_requires_author(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    _, frame = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    with pytest.raises(StoryForbiddenError):
        await service.delete_frame(frame_id=frame.id, actor_user_id="u2")
    # Author can delete it.
    await service.delete_frame(frame_id=frame.id, actor_user_id="u1")


async def test_delete_story_requires_author(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    story, _ = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    with pytest.raises(StoryForbiddenError):
        await service.delete_story(story_id=story.id, actor_user_id="u2")
    await service.delete_story(story_id=story.id, actor_user_id="u1")


async def test_react_or_view_unknown_frame_raises(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    with pytest.raises(StoryNotFoundError):
        await service.mark_frame_viewed(frame_id="nope", viewer_user_id="u1")
    with pytest.raises(StoryNotFoundError):
        await service.react_to_frame(frame_id="nope", reactor_user_id="u1", emoji="🔥")
    with pytest.raises(StoryNotFoundError):
        await service.clear_reaction(frame_id="nope", reactor_user_id="u1")


async def test_explicit_audience_kind_overrides_prefs(db, svc):
    """Caller-supplied audience overrides the author's default."""
    service, _ = svc
    await _seed_user(
        db,
        "u1",
        "pascal",
        prefs_json='{"stories": {"default_audience": {"kind": "all_paired"}}}',
    )
    story, _ = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/x.webp",
        audience_kind=StoryAudience.HOUSEHOLDS,
        audience=("inst-b",),
    )
    assert story.audience_kind is StoryAudience.HOUSEHOLDS
    assert story.audience == ("inst-b",)


async def test_get_with_frames_and_get_frame(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    story, frame = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    pair = await service.get_with_frames(story.id)
    assert pair is not None
    s, frames = pair
    assert s.id == story.id and len(frames) == 1
    f = await service.get_frame(frame.id)
    assert f is not None and f.id == frame.id
    # Missing ids return None / empty.
    assert await service.get_with_frames("missing") is None
    assert await service.get_frame("missing") is None


async def test_share_to_feed_household(db, svc):
    """share_to_feed routes household scope through feed_service.create_post."""
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    story, _ = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )

    class FakeFeedService:
        def __init__(self):
            self.calls = []

        async def create_post(self, **kwargs):
            self.calls.append(kwargs)
            return type("P", (), {"id": "post-1"})()

    feed = FakeFeedService()
    post = await service.share_to_feed(
        story_id=story.id,
        actor_user_id="u1",
        scope="household",
        space_id=None,
        note="hello",
        feed_service=feed,
        space_service=None,
    )
    assert post.id == "post-1"
    assert feed.calls[0]["type"] == "story_share"
    assert feed.calls[0]["linked_story_id"] == story.id


async def test_share_to_feed_rejects_non_author(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    story, _ = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    with pytest.raises(StoryForbiddenError):
        await service.share_to_feed(
            story_id=story.id,
            actor_user_id="u2",
            scope="household",
            space_id=None,
            note=None,
            feed_service=None,
            space_service=None,
        )


async def test_share_to_feed_unknown_scope(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    story, _ = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    with pytest.raises(ValueError):
        await service.share_to_feed(
            story_id=story.id,
            actor_user_id="u1",
            scope="bogus",
            space_id=None,
            note=None,
            feed_service=None,
            space_service=None,
        )


async def test_dm_reply_to_frame_calls_dm_service_with_snapshot(db, svc):
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    _, frame = await service.create_or_append_frame(
        author_user_id="u1",
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/x.webp",
        caption_text="from the trip",
        caption_emoji="🏖",
    )

    class FakeDmService:
        def __init__(self):
            self.kwargs = None

        async def send_message(self, conversation_id, **kw):
            self.kwargs = {"conversation_id": conversation_id, **kw}
            return type("M", (), {"id": "msg-1"})()

    dm = FakeDmService()
    msg = await service.dm_reply_to_frame(
        frame_id=frame.id,
        sender_user_id="u2",
        conversation_id="conv-1",
        content="lol nice",
        dm_service=dm,
    )
    assert msg.id == "msg-1"
    assert dm.kwargs["sender_username"] == "maria"
    assert dm.kwargs["reply_to_story_frame_id"] == frame.id
    snapshot_json = dm.kwargs["reply_to_story_frame_snapshot"]
    assert "x.webp" in snapshot_json
    assert "🏖" in snapshot_json


async def test_expire_due_drops_expired(db, svc):
    """``expire_due`` runs prune_expired and reports counts."""
    service, _ = svc
    await _seed_user(db, "u1", "pascal")
    repo = service._stories
    # Inject one stale row by hand so prune_expired finds work.
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=StoryAudience.ALL_PAIRED,
        audience=(),
        story_date="2026-01-01",
        expires_at=past,
    )
    expired, over_max = await service.expire_due()
    assert expired == 1
    assert over_max == 0  # max_count default (100) is well above one row
