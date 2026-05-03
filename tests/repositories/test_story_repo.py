"""Repository-level tests for the stories tables."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.story import StoryAudience, StoryFrameType
from socialhome.repositories.story_repo import SqliteStoryRepo


async def _seed_user(db, user_id: str = "u1", username: str = "pascal") -> None:
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES(?,?,?, 'active')",
        (user_id, username, username.title()),
    )


@pytest.fixture
async def repo(db):
    await db.enqueue("INSERT OR IGNORE INTO household_features(id) VALUES('default')")
    return SqliteStoryRepo(db)


def _expires(days: int = 7) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


async def test_find_or_create_today_is_idempotent(db, repo):
    await _seed_user(db)
    story1 = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=StoryAudience.ALL_PAIRED,
        audience=(),
        story_date="2026-05-03",
        expires_at=_expires(),
    )
    story2 = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=StoryAudience.ALL_PAIRED,
        audience=(),
        story_date="2026-05-03",
        expires_at=_expires(),
    )
    assert story1.id == story2.id


async def test_append_frame_assigns_sequential_numbers(db, repo):
    await _seed_user(db)
    story = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=StoryAudience.ALL_PAIRED,
        audience=(),
        story_date="2026-05-03",
        expires_at=_expires(),
    )
    f1 = await repo.append_frame(
        story_id=story.id,
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/a.webp",
    )
    f2 = await repo.append_frame(
        story_id=story.id,
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/b.webp",
    )
    assert f1.sequence == 1
    assert f2.sequence == 2
    frames = await repo.list_frames(story.id)
    assert [f.sequence for f in frames] == [1, 2]


async def test_view_and_reaction_round_trip(db, repo):
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    story = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=StoryAudience.ALL_PAIRED,
        audience=(),
        story_date="2026-05-03",
        expires_at=_expires(),
    )
    frame = await repo.append_frame(
        story_id=story.id,
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    await repo.mark_viewed(frame.id, "u2")
    views = await repo.list_views_for_frame(frame.id)
    assert len(views) == 1 and views[0].viewer_user_id == "u2"

    await repo.set_reaction(frame.id, "u2", "🔥")
    rs = await repo.list_reactions_for_frame(frame.id)
    assert len(rs) == 1 and rs[0].emoji == "🔥"
    # Changing the emoji upserts (still one row).
    await repo.set_reaction(frame.id, "u2", "❤️")
    rs = await repo.list_reactions_for_frame(frame.id)
    assert len(rs) == 1 and rs[0].emoji == "❤️"


async def test_prune_expired_drops_only_old_rows(db, repo):
    await _seed_user(db)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    future = _expires(7)
    expired = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=StoryAudience.ALL_PAIRED,
        audience=(),
        story_date="2026-04-01",
        expires_at=past,
    )
    fresh = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=StoryAudience.ALL_PAIRED,
        audience=(),
        story_date="2026-05-03",
        expires_at=future,
    )
    pruned = await repo.prune_expired()
    assert pruned == 1
    assert await repo.get_story(expired.id) is None
    assert await repo.get_story(fresh.id) is not None


async def test_visibility_filters_users_audience(db, repo):
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    await _seed_user(db, "u3", "lina")
    # USERS-kind story aimed at u2 only.
    await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=StoryAudience.USERS,
        audience=("u2",),
        story_date="2026-05-03",
        expires_at=_expires(),
    )
    seen_by_u2 = await repo.list_visible_to("u2")
    seen_by_u3 = await repo.list_visible_to("u3")
    seen_by_u1 = await repo.list_visible_to("u1")  # author always sees own
    assert len(seen_by_u2) == 1
    assert len(seen_by_u3) == 0
    assert len(seen_by_u1) == 1
