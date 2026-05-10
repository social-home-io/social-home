"""Tests for socialhome.repositories.peer_user_visibility_repo."""

from __future__ import annotations

import pytest

from socialhome.repositories.peer_user_visibility_repo import (
    SqlitePeerUserVisibilityRepo,
)


@pytest.fixture
async def repo(tmp_dir):
    from socialhome.db.database import AsyncDatabase

    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    # The FK targets (``remote_instances`` / ``users``) need stub rows
    # so the inserts succeed under PRAGMA foreign_keys=ON.
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk, "
        "key_self_to_remote, key_remote_to_self, remote_inbox_url, "
        "local_inbox_id, status) "
        "VALUES('peer-1','Peer One','aa','enc','enc','https://p1','wh-1','confirmed')",
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES('alice','uid-alice','Alice',1)",
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES('lily','uid-lily','Lily',0)",
    )
    yield SqlitePeerUserVisibilityRepo(db)
    await db.shutdown()


async def test_is_visible_defaults_true_when_no_row(repo):
    assert await repo.is_visible("peer-1", "uid-alice") is True
    assert await repo.is_visible("peer-1", "uid-lily") is True


async def test_set_visibility_then_is_visible_returns_value(repo):
    await repo.set_visibility(
        instance_id="peer-1",
        user_id="uid-lily",
        visible=False,
        set_by="uid-alice",
    )
    assert await repo.is_visible("peer-1", "uid-lily") is False
    # Other users / peers stay default-visible.
    assert await repo.is_visible("peer-1", "uid-alice") is True
    assert await repo.is_visible("other-peer", "uid-lily") is True


async def test_set_visibility_upserts_in_place(repo):
    await repo.set_visibility(
        instance_id="peer-1", user_id="uid-lily", visible=False, set_by=None,
    )
    await repo.set_visibility(
        instance_id="peer-1", user_id="uid-lily", visible=True, set_by="uid-alice",
    )
    assert await repo.is_visible("peer-1", "uid-lily") is True
    rows = await repo.list_for_peer("peer-1")
    # One row, flipped — not two.
    matching = [r for r in rows if r.user_id == "uid-lily"]
    assert len(matching) == 1
    assert matching[0].visible is True
    assert matching[0].set_by == "uid-alice"


async def test_hidden_user_ids_for_peer_returns_only_hidden(repo):
    await repo.set_visibility(
        instance_id="peer-1", user_id="uid-lily", visible=False, set_by=None,
    )
    # Explicit-allow row should NOT show up in the hidden set.
    await repo.set_visibility(
        instance_id="peer-1", user_id="uid-alice", visible=True, set_by=None,
    )
    hidden = await repo.hidden_user_ids_for_peer("peer-1")
    assert hidden == frozenset({"uid-lily"})


async def test_list_for_peer_returns_all_rows_for_that_peer(repo):
    await repo.set_visibility(
        instance_id="peer-1", user_id="uid-lily", visible=False, set_by=None,
    )
    await repo.set_visibility(
        instance_id="peer-1", user_id="uid-alice", visible=True, set_by=None,
    )
    rows = await repo.list_for_peer("peer-1")
    assert {r.user_id for r in rows} == {"uid-lily", "uid-alice"}


async def test_list_for_peer_empty_for_unknown_peer(repo):
    rows = await repo.list_for_peer("unknown-peer")
    assert rows == []


async def test_hidden_user_ids_for_peer_empty_when_no_rows(repo):
    assert await repo.hidden_user_ids_for_peer("peer-1") == frozenset()
