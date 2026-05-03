"""Repository-level tests for the stories tables."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.story import (
    Story,
    StoryAudience,
    StoryFrame,
    StoryFrameType,
)
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


async def test_count_unseen_frames_drops_to_zero_after_view(db, repo):
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
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
    assert await repo.count_unseen_frames(story.id, "u2") == 2
    await repo.mark_viewed(f1.id, "u2")
    assert await repo.count_unseen_frames(story.id, "u2") == 1
    await repo.mark_viewed(f2.id, "u2")
    assert await repo.count_unseen_frames(story.id, "u2") == 0


async def test_save_story_and_save_frame_upsert(db, repo):
    """Federation upsert path: caller-supplied id is preserved."""
    await _seed_user(db)
    s = Story(
        id="story-remote-1",
        author_user_id="u1",
        story_date="2026-05-03",
        audience_kind=StoryAudience.ALL_PAIRED,
        audience=(),
        created_at="2026-05-03T08:00:00+00:00",
        expires_at=_expires(),
    )
    out = await repo.save_story(s)
    assert out.id == "story-remote-1"
    assert (await repo.get_story("story-remote-1")) is not None
    f = StoryFrame(
        id="frame-remote-1",
        story_id="story-remote-1",
        sequence=1,
        frame_type=StoryFrameType.VIDEO,
        media_url="/api/media/v.mp4",
        duration_ms=4500,
    )
    await repo.save_frame(f)
    fetched = await repo.get_frame("frame-remote-1")
    assert fetched is not None
    assert fetched.frame_type is StoryFrameType.VIDEO
    assert fetched.duration_ms == 4500


async def test_clear_reaction_and_delete_frame(db, repo):
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
        media_url="/api/media/a.webp",
    )
    await repo.set_reaction(frame.id, "u2", "🔥")
    await repo.clear_reaction(frame.id, "u2")
    assert await repo.list_reactions_for_frame(frame.id) == []
    await repo.delete_frame(frame.id)
    assert await repo.get_frame(frame.id) is None


async def test_prune_over_max_drops_oldest(db, repo):
    await _seed_user(db)
    # Three stories on different days; max_count=1 keeps the newest.
    for d in ("2026-05-01", "2026-05-02", "2026-05-03"):
        await repo.find_or_create_today(
            author_user_id="u1",
            audience_kind=StoryAudience.ALL_PAIRED,
            audience=(),
            story_date=d,
            expires_at=_expires(),
        )
    pruned = await repo.prune_over_max("u1", max_count=1)
    assert pruned == 2
    rest = await repo.list_authored("u1")
    assert len(rest) == 1
    assert rest[0].story_date == "2026-05-03"


async def test_list_authors_with_stories(db, repo):
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    for uid in ("u1", "u2", "u1"):  # u1 twice → still listed once
        await repo.find_or_create_today(
            author_user_id=uid,
            audience_kind=StoryAudience.ALL_PAIRED,
            audience=(),
            story_date={"u1": "2026-05-03", "u2": "2026-05-04"}[uid],
            expires_at=_expires(),
        )
    authors = sorted(await repo.list_authors_with_stories())
    assert authors == ["u1", "u2"]
