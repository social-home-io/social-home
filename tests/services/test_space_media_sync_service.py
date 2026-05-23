"""Tests for the space-post media bytes outbox + scheduler."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import FederationEventType
from socialhome.repositories.space_media_outbox_repo import (
    SqliteSpaceMediaOutboxRepo,
)
from socialhome.services.space_media_sync_service import (
    MAX_BLOB_CHUNK_BYTES,
    SINGLE_CHUNK_BYTES_THRESHOLD,
    SpaceMediaSyncService,
)


async def _seed_space_post(db, *, post_id="p-1", space_id="sp-1"):
    """Insert a parent space + space_posts row so FK doesn't trip."""
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (space_id, "S", "peer", "owner", "aa" * 32, "household", "invite_only"),
    )
    await db.enqueue(
        """INSERT INTO space_posts(id, space_id, author, type, created_at)
           VALUES(?,?,?,?,?)""",
        (post_id, space_id, "u", "image", "2026-05-23T00:00:00+00:00"),
    )


@pytest.fixture
async def db(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    yield db
    await db.shutdown()


@pytest.fixture
async def outbox(db):
    return SqliteSpaceMediaOutboxRepo(db)


async def test_enqueue_for_post_writes_one_row_per_peer_per_url(
    db,
    outbox,
    tmp_path,
):
    """``enqueue_for_post`` writes a row per (URL, peer) tuple."""
    await _seed_space_post(db)
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=None,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_post(
        post_id="p-1",
        target_instance_ids=["peer-a", "peer-b"],
        media_urls=["api/media/img-1.webp", "api/media/img-2.webp"],
    )
    rows = await outbox.list_for_post("p-1")
    # 2 urls × 2 peers = 4 rows.
    assert len(rows) == 4
    by_pair = {(r.blob_id, r.target_instance_id) for r in rows}
    assert by_pair == {
        ("img-1.webp", "peer-a"),
        ("img-1.webp", "peer-b"),
        ("img-2.webp", "peer-a"),
        ("img-2.webp", "peer-b"),
    }


async def test_flush_once_ships_small_file_as_single_chunk(
    db,
    outbox,
    tmp_path,
):
    """A file ≤ ``SINGLE_CHUNK_BYTES_THRESHOLD`` ships in one
    SPACE_MEDIA_BLOB with ``chunk_count=1, final=True``."""
    await _seed_space_post(db)
    # Small WebP under the threshold.
    body = b"WEBP-small-bytes-" * 8  # < 1 MiB
    (tmp_path / "small.webp").write_bytes(body)
    fed = AsyncMock()
    fed.send_event = AsyncMock()
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=fed,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_post(
        post_id="p-1",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/small.webp"],
    )
    shipped = await svc.flush_once()
    assert shipped == 1
    fed.send_event.assert_awaited_once()
    call = fed.send_event.call_args
    assert call.kwargs["to_instance_id"] == "peer-x"
    assert call.kwargs["event_type"] is FederationEventType.SPACE_MEDIA_BLOB
    payload = call.kwargs["payload"]
    assert payload["filename"] == "small.webp"
    assert payload["chunk_count"] == 1
    assert payload["chunk_index"] == 0
    assert payload["final"] is True
    assert base64.b64decode(payload["bytes_b64"]) == body
    # Row gone after success.
    assert await outbox.list_for_post("p-1") == []


async def test_flush_once_chunks_large_file(db, outbox, tmp_path):
    """A file > ``SINGLE_CHUNK_BYTES_THRESHOLD`` ships as multiple
    ``MAX_BLOB_CHUNK_BYTES`` chunks with strictly-increasing index."""
    await _seed_space_post(db)
    # Build a file size that exceeds the single-chunk threshold AND
    # splits cleanly into multiple chunks. Pick 5 chunks worth + a
    # remainder so the size is firmly above ``SINGLE_CHUNK_BYTES_THRESHOLD``.
    size = MAX_BLOB_CHUNK_BYTES * 5 + 17
    assert size > SINGLE_CHUNK_BYTES_THRESHOLD
    body = bytes(range(256)) * (size // 256 + 1)
    body = body[:size]
    (tmp_path / "big.webm").write_bytes(body)
    fed = AsyncMock()
    fed.send_event = AsyncMock()
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=fed,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_post(
        post_id="p-1",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/big.webm"],
    )
    shipped = await svc.flush_once()
    assert shipped == 1
    # Expected chunk count = ceil(size / MAX_BLOB_CHUNK_BYTES) = 6.
    assert fed.send_event.await_count == 6
    payloads = [c.kwargs["payload"] for c in fed.send_event.call_args_list]
    assert [p["chunk_index"] for p in payloads] == [0, 1, 2, 3, 4, 5]
    assert all(p["chunk_count"] == 6 for p in payloads)
    assert [p["final"] for p in payloads] == [
        False,
        False,
        False,
        False,
        False,
        True,
    ]
    reassembled = b"".join(base64.b64decode(p["bytes_b64"]) for p in payloads)
    assert reassembled == body


async def test_flush_once_no_federation_returns_zero(db, outbox, tmp_path):
    """Without federation attached, flush is a no-op."""
    await _seed_space_post(db)
    (tmp_path / "x.webp").write_bytes(b"x")
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=None,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_post(
        post_id="p-1",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/x.webp"],
    )
    assert await svc.flush_once() == 0
    # Row still pending.
    assert len(await outbox.list_for_post("p-1")) == 1


async def test_flush_once_missing_file_reschedules(db, outbox, tmp_path):
    """If the file is gone (user deleted post media), the row
    reschedules with backoff rather than crashing."""
    await _seed_space_post(db)
    fed = AsyncMock()
    fed.send_event = AsyncMock()
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=fed,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_post(
        post_id="p-1",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/ghost.webp"],
    )
    shipped = await svc.flush_once()
    assert shipped == 0
    fed.send_event.assert_not_awaited()
    # Row remains for next attempt.
    rows = await outbox.list_for_post("p-1")
    assert len(rows) == 1
    assert rows[0].attempts == 1
    assert rows[0].last_error is not None


async def test_dedup_same_blob_to_same_peer(db, outbox, tmp_path):
    """ON CONFLICT DO NOTHING — re-enqueueing the same
    (blob_id, peer) tuple from a second post that references the
    same image doesn't create a duplicate row."""
    await _seed_space_post(db, post_id="p-a")
    # Second post under SAME space.
    await db.enqueue(
        """INSERT INTO space_posts(id, space_id, author, type, created_at)
           VALUES(?,?,?,?,?)""",
        ("p-b", "sp-1", "u", "image", "2026-05-23T00:01:00+00:00"),
    )
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=None,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_post(
        post_id="p-a",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/shared.webp"],
    )
    await svc.enqueue_for_post(
        post_id="p-b",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/shared.webp"],
    )
    # ON CONFLICT(blob_id, target_instance_id) DO NOTHING.
    rows_a = await outbox.list_for_post("p-a")
    rows_b = await outbox.list_for_post("p-b")
    assert len(rows_a) == 1
    assert len(rows_b) == 0
