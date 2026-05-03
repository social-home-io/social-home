"""Smoke tests for :class:`StoryRetentionScheduler`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.story import StoryAudience, StoryFrameType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.story_retention_scheduler import (
    StoryRetentionScheduler,
)
from socialhome.repositories.story_repo import SqliteStoryRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.story_service import StoryService


async def _seed_user(db) -> None:
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state, "
        "preferences_json) VALUES('u1','pascal','Pascal','active','{}')",
    )


async def test_expire_due_drops_expired_rows(db):
    await _seed_user(db)
    bus = EventBus()
    repo = SqliteStoryRepo(db)
    user_repo = SqliteUserRepo(db)
    service = StoryService(repo, user_repo, bus)

    # Hand-craft a story with expires_at in the past so the prune fires.
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    expired = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=StoryAudience.ALL_PAIRED,
        audience=(),
        story_date="2026-01-01",
        expires_at=past,
    )
    await repo.append_frame(
        story_id=expired.id,
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    expired_count, _ = await service.expire_due()
    assert expired_count >= 1
    assert await repo.get_story(expired.id) is None


@pytest.mark.asyncio
async def test_scheduler_start_and_stop_are_idempotent(db):
    await _seed_user(db)
    bus = EventBus()
    service = StoryService(SqliteStoryRepo(db), SqliteUserRepo(db), bus)
    sched = StoryRetentionScheduler(service, interval_seconds=3600)
    await sched.start()
    await sched.start()  # idempotent — should not error
    await sched.stop()
    await sched.stop()  # idempotent
