"""Tests for socialhome.repositories.moment_repo."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.moment import Moment
from socialhome.repositories.moment_repo import SqliteMomentRepo


def _now_iso(offset_minutes: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)).isoformat()


def _moment(
    *,
    id: str = "m-1",
    author: str = "u-author",
    content: str = "hello",
    parent_moment_id: str | None = None,
    created_offset_min: int = 0,
    expires_days: int = 7,
) -> Moment:
    return Moment(
        id=id,
        author_user_id=author,
        content=content,
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=parent_moment_id,
        origin_instance_id="self",
        created_at=_now_iso(created_offset_min),
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=expires_days)
        ).isoformat(),
    )


@pytest.fixture
async def repo(db):
    await db.enqueue("INSERT OR IGNORE INTO household_features(id) VALUES('default')")
    return SqliteMomentRepo(db)


async def test_save_get_round_trip(db, repo):
    m = _moment()
    await repo.save(m)
    got = await repo.get("m-1")
    assert got is not None
    assert got.author_user_id == "u-author"
    assert got.content == "hello"


async def test_save_is_upsert(db, repo):
    """Inbound federation handlers may receive the same moment twice
    via different relay paths; the second save must not duplicate."""
    await repo.save(_moment(id="m-up", content="v1"))
    await repo.save(_moment(id="m-up", content="v2"))
    got = await repo.get("m-up")
    assert got is not None
    assert got.content == "v2"


async def test_list_visible_filters_blocked_authors(db, repo):
    await repo.save(_moment(id="m-a", author="u-bad"))
    await repo.save(_moment(id="m-b", author="u-good"))
    # Viewer u-me has u-bad on their block list.
    await db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?, ?)",
        ("u-me", "u-bad"),
    )
    visible = await repo.list_visible_to("u-me")
    assert {m.id for m in visible} == {"m-b"}


async def test_list_visible_24h_default_then_7d_for_followers(db, repo):
    """A 36-h-old moment is hidden by default; if the viewer follows
    the author it surfaces (within the 7-day absolute cap)."""
    old = _moment(id="m-old", author="u-bob", created_offset_min=-36 * 60)
    await repo.save(old)
    # u-me does NOT follow u-bob → not visible.
    assert await repo.list_visible_to("u-me") == []
    # After follow → visible.
    await db.enqueue(
        "INSERT INTO user_follows(follower_user_id, followed_user_id) VALUES(?, ?)",
        ("u-me", "u-bob"),
    )
    visible = await repo.list_visible_to("u-me")
    assert [m.id for m in visible] == ["m-old"]


async def test_list_visible_drops_past_absolute_expiry(db, repo):
    """Even followers can't see moments past the 7-day expiry."""
    expired = Moment(
        id="m-expired",
        author_user_id="u-bob",
        content="ancient",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=None,
        origin_instance_id="self",
        created_at=_now_iso(-9 * 24 * 60),
        expires_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
    )
    await repo.save(expired)
    await db.enqueue(
        "INSERT INTO user_follows(follower_user_id, followed_user_id) VALUES(?, ?)",
        ("u-me", "u-bob"),
    )
    assert await repo.list_visible_to("u-me") == []


async def test_list_replies(db, repo):
    await repo.save(_moment(id="m-root", author="u-author"))
    await repo.save(_moment(id="m-r1", author="u-other", parent_moment_id="m-root"))
    await repo.save(_moment(id="m-r2", author="u-other2", parent_moment_id="m-root"))
    replies = await repo.list_replies("m-root")
    assert {r.id for r in replies} == {"m-r1", "m-r2"}


async def test_count_recent_for_author_excludes_replies(db, repo):
    """The 15-min rate-limit ignores replies — they're spam-free."""
    await repo.save(_moment(id="m-top", author="u-bob"))
    await repo.save(_moment(id="m-r", author="u-bob", parent_moment_id="m-top"))
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    n = await repo.count_recent_for_author("u-bob", since_iso=since)
    assert n == 1


async def test_set_and_clear_reaction(db, repo):
    await repo.save(_moment(id="m-r"))
    await repo.set_reaction("m-r", "u-rx", "🔥")
    rs = await repo.list_reactions("m-r")
    assert [r.emoji for r in rs] == ["🔥"]
    # Same reactor changing emoji upserts.
    await repo.set_reaction("m-r", "u-rx", "❤️")
    rs = await repo.list_reactions("m-r")
    assert len(rs) == 1 and rs[0].emoji == "❤️"
    # Clear removes.
    await repo.clear_reaction("m-r", "u-rx")
    assert await repo.list_reactions("m-r") == []


async def test_prune_expired_drops_past_cap(db, repo):
    fresh = _moment(id="m-fresh")
    expired = Moment(
        id="m-expired",
        author_user_id="u-bob",
        content="old",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=None,
        origin_instance_id="self",
        created_at=_now_iso(-9 * 24 * 60),
        expires_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
    )
    await repo.save(fresh)
    await repo.save(expired)
    pruned = await repo.prune_expired()
    assert pruned == 1
    assert await repo.get("m-fresh") is not None
    assert await repo.get("m-expired") is None
