"""Service-level tests for the stories pillar."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.events import StoryFrameAdded, StoryFrameViewed
from socialhome.domain.story import StoryAudience, StoryFrameType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.story_repo import SqliteStoryRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.story_service import (
    MAX_FRAMES_PER_STORY,
    StoryFrameLimitError,
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
