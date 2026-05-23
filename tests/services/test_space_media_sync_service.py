"""Tests for the space-post + gallery media bytes outbox + scheduler."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import DeliveryResult, FederationEventType
from socialhome.repositories.space_media_outbox_repo import (
    SqliteSpaceMediaOutboxRepo,
)
from socialhome.services.space_media_sync_service import (
    MAX_BLOB_CHUNK_BYTES,
    SINGLE_CHUNK_BYTES_THRESHOLD,
    SpaceMediaSyncService,
)


#: Successful-delivery shape the scheduler treats as a green ship.
_OK = DeliveryResult(instance_id="peer-x", ok=True)


async def _seed_space(db, *, space_id="sp-1"):
    """Insert a parent ``spaces`` row so the outbox FK doesn't trip."""
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (space_id, "S", "peer", "owner", "aa" * 32, "household", "invite_only"),
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


async def test_enqueue_for_blob_writes_one_row_per_peer_per_url(
    db,
    outbox,
    tmp_path,
):
    """One row per (URL, peer) tuple — gallery items and posts both
    use this surface."""
    await _seed_space(db)
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=None,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-a", "peer-b"],
        media_urls=["api/media/img-1.webp", "api/media/img-2.webp"],
    )
    rows = await outbox.list_for_correlation("p-1")
    assert len(rows) == 4
    by_pair = {(r.blob_id, r.target_instance_id) for r in rows}
    assert by_pair == {
        ("img-1.webp", "peer-a"),
        ("img-1.webp", "peer-b"),
        ("img-2.webp", "peer-a"),
        ("img-2.webp", "peer-b"),
    }
    # Every row carries the space scope so a SPACE_DISSOLVED cascades.
    assert all(r.space_id == "sp-1" for r in rows)
    # Every row carries the soft correlation_id.
    assert all(r.correlation_id == "p-1" for r in rows)


async def test_flush_once_ships_small_file_as_single_chunk(
    db,
    outbox,
    tmp_path,
):
    """A file ≤ ``SINGLE_CHUNK_BYTES_THRESHOLD`` ships as one chunk."""
    await _seed_space(db)
    body = b"WEBP-small-bytes-" * 8
    (tmp_path / "small.webp").write_bytes(body)
    fed = AsyncMock()
    fed.send_with_mesh_fallback = AsyncMock(return_value=_OK)
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=fed,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/small.webp"],
    )
    shipped = await svc.flush_once()
    assert shipped == 1
    fed.send_with_mesh_fallback.assert_awaited_once()
    call = fed.send_with_mesh_fallback.call_args
    assert call.kwargs["to_instance_id"] == "peer-x"
    assert call.kwargs["event_type"] is FederationEventType.SPACE_MEDIA_BLOB
    payload = call.kwargs["payload"]
    assert payload["filename"] == "small.webp"
    assert payload["chunk_count"] == 1
    assert payload["chunk_index"] == 0
    assert payload["final"] is True
    assert payload["correlation_id"] == "p-1"
    assert payload["space_id"] == "sp-1"
    assert base64.b64decode(payload["bytes_b64"]) == body
    assert await outbox.list_for_correlation("p-1") == []


async def test_flush_once_chunks_large_file(db, outbox, tmp_path):
    """Files above ``SINGLE_CHUNK_BYTES_THRESHOLD`` split into
    ``MAX_BLOB_CHUNK_BYTES`` chunks with strictly increasing index."""
    await _seed_space(db)
    size = MAX_BLOB_CHUNK_BYTES * 5 + 17
    assert size > SINGLE_CHUNK_BYTES_THRESHOLD
    body = bytes(range(256)) * (size // 256 + 1)
    body = body[:size]
    (tmp_path / "big.webm").write_bytes(body)
    fed = AsyncMock()
    fed.send_with_mesh_fallback = AsyncMock(return_value=_OK)
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=fed,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/big.webm"],
    )
    shipped = await svc.flush_once()
    assert shipped == 1
    assert fed.send_with_mesh_fallback.await_count == 6
    payloads = [c.kwargs["payload"] for c in fed.send_with_mesh_fallback.call_args_list]
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
    await _seed_space(db)
    (tmp_path / "x.webp").write_bytes(b"x")
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=None,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/x.webp"],
    )
    assert await svc.flush_once() == 0
    assert len(await outbox.list_for_correlation("p-1")) == 1


async def test_flush_once_missing_file_reschedules(db, outbox, tmp_path):
    """File missing → row reschedules with backoff, not a crash."""
    await _seed_space(db)
    fed = AsyncMock()
    fed.send_with_mesh_fallback = AsyncMock(return_value=_OK)
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=fed,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/ghost.webp"],
    )
    shipped = await svc.flush_once()
    assert shipped == 0
    fed.send_with_mesh_fallback.assert_not_awaited()
    rows = await outbox.list_for_correlation("p-1")
    assert len(rows) == 1
    assert rows[0].attempts == 1
    assert rows[0].last_error is not None


async def test_flush_once_reschedules_on_delivery_failure(
    db,
    outbox,
    tmp_path,
):
    """``send_with_mesh_fallback`` returns ``ok=False`` (no_route /
    not_confirmed / transport blip) — the scheduler must reschedule
    rather than treat that as a success."""
    await _seed_space(db)
    (tmp_path / "x.webp").write_bytes(b"x")
    fed = AsyncMock()
    fed.send_with_mesh_fallback = AsyncMock(
        return_value=DeliveryResult(
            instance_id="peer-x",
            ok=False,
            error="no_route",
        ),
    )
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=fed,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/x.webp"],
    )
    shipped = await svc.flush_once()
    assert shipped == 0
    rows = await outbox.list_for_correlation("p-1")
    assert len(rows) == 1
    assert rows[0].attempts == 1
    assert rows[0].last_error == "no_route"


async def test_dedup_same_blob_to_same_peer(db, outbox, tmp_path):
    """ON CONFLICT(blob_id, target_instance_id) DO NOTHING — same
    blob referenced by two different correlation ids deduplicates."""
    await _seed_space(db)
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=None,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="post-a",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/shared.webp"],
    )
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="gallery-b",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/shared.webp"],
    )
    rows_a = await outbox.list_for_correlation("post-a")
    rows_b = await outbox.list_for_correlation("gallery-b")
    assert len(rows_a) == 1
    assert len(rows_b) == 0


async def test_enqueue_for_post_back_compat_signature(db, outbox, tmp_path):
    """The ``enqueue_for_post`` alias kept for PR #440 call sites
    still works — it's a thin wrapper around ``enqueue_for_blob``."""
    await _seed_space(db)
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=None,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_post(
        post_id="p-back-compat",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/x.webp"],
        space_id="sp-1",
    )
    rows = await outbox.list_for_correlation("p-back-compat")
    assert len(rows) == 1
    assert rows[0].correlation_id == "p-back-compat"
    assert rows[0].space_id == "sp-1"
