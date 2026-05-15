"""Receiver-side ``DM_MEDIA_BLOB`` handling — coverage for the
preview-now-sync-later flow's inbound half.

Exercises the chunk-buffer-and-concat path, the back-compat
single-chunk shape, the blob-before-message reordering guard,
and the MIME-magic-byte sniff that flags suspicious payloads
without dropping the file.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

import pytest

from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.repositories import (
    SqliteConversationRepo,
    SqliteSpacePostRepo,
    SqliteSpaceRepo,
    SqliteUserRepo,
)
from socialhome.services.federation_inbound_service import (
    FederationInboundService,
    _bytes_match_mime,
    _mime_to_ext,
)


pytestmark = pytest.mark.asyncio


_WEBP_HEADER = b"\x52\x49\x46\x46\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 32


def _event(event_type, payload):
    return FederationEvent(
        msg_id="msg-" + event_type.value,
        event_type=event_type,
        from_instance="peer-a",
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
    )


@pytest.fixture
async def inbound_with_media(db, bus, tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        media_dir=media_dir,
    )
    # Seed a conversation + message row that the blob handler can
    # update.
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?, datetime('now'))",
        ("conv-1", "dm"),
    )
    await db.enqueue(
        """
        INSERT INTO conversation_messages(
            id, conversation_id, sender_user_id, content, type,
            media_blob_id, media_sync_status, created_at
        ) VALUES(?,?,?,?,?,?,?, datetime('now'))
        """,
        ("m-1", "conv-1", "user-remote", "", "image", "m-1", "pending"),
    )
    return svc, media_dir, db


# ── Single-chunk (back-compat) blob ────────────────────────────────────


async def test_dm_media_blob_single_chunk_writes_and_swaps(inbound_with_media):
    """Legacy single-payload shape (no chunk fields) writes the
    file straight to ``<msg_id>.<ext>`` and clears the row's
    ``media_sync_status``.
    """
    svc, media_dir, db = inbound_with_media
    await svc._on_dm_media_blob(
        _event(
            FederationEventType.DM_MEDIA_BLOB,
            {
                "media_blob_id": "m-1",
                "message_id": "m-1",
                "conversation_id": "conv-1",
                "mime_type": "image/webp",
                "bytes_b64": base64.b64encode(_WEBP_HEADER).decode("ascii"),
            },
        )
    )
    dest = media_dir / "m-1.webp"
    assert dest.is_file()
    assert dest.read_bytes() == _WEBP_HEADER

    row = await db.fetchone(
        "SELECT media_url, media_sync_status FROM conversation_messages WHERE id=?",
        ("m-1",),
    )
    assert row is not None
    assert row["media_url"] == "api/media/m-1.webp"
    assert row["media_sync_status"] is None  # cleared


async def test_dm_media_blob_missing_fields_drops(inbound_with_media):
    """Payloads without the required fields are dropped (no crash)."""
    svc, _media_dir, _db = inbound_with_media
    # No bytes_b64 — fall through.
    await svc._on_dm_media_blob(
        _event(
            FederationEventType.DM_MEDIA_BLOB,
            {"media_blob_id": "m-1", "message_id": "m-1"},
        )
    )
    # No raise = pass.


async def test_dm_media_blob_skips_when_media_dir_unwired(db, bus):
    """A service constructed without media_dir tolerates the event."""
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        media_dir=None,
    )
    await svc._on_dm_media_blob(
        _event(
            FederationEventType.DM_MEDIA_BLOB,
            {
                "media_blob_id": "m-2",
                "message_id": "m-2",
                "bytes_b64": base64.b64encode(b"x").decode("ascii"),
            },
        )
    )
    # No raise = pass.


# ── Chunked payload ─────────────────────────────────────────────────────


async def test_dm_media_blob_chunked_writes_parts_then_concats(
    inbound_with_media,
):
    """Three-chunk payload: each ``part<idx>`` lands, final
    triggers concat, parts are cleaned up, row updates."""
    svc, media_dir, db = inbound_with_media
    full = b"chunk0_" + b"chunk1_" + b"chunk2_"
    parts = [b"chunk0_", b"chunk1_", b"chunk2_"]
    for i, p in enumerate(parts):
        await svc._on_dm_media_blob(
            _event(
                FederationEventType.DM_MEDIA_BLOB,
                {
                    "media_blob_id": "m-1",
                    "message_id": "m-1",
                    "conversation_id": "conv-1",
                    "mime_type": "image/webp",
                    "chunk_index": i,
                    "chunk_count": 3,
                    "final": i == 2,
                    "bytes_b64": base64.b64encode(p).decode("ascii"),
                },
            )
        )
        if i < 2:
            # Pre-final chunks land as part files, no concat yet.
            assert (media_dir / f"m-1.part{i:05d}").is_file()
            assert not (media_dir / "m-1.webp").is_file()
    # Final chunk → concat, part files removed.
    dest = media_dir / "m-1.webp"
    assert dest.is_file()
    # Bytes don't match a real WebP so the sniff flags failed,
    # but the file still lands. The content is the concat though.
    assert dest.read_bytes() == full
    for i in range(3):
        assert not (media_dir / f"m-1.part{i:05d}").exists()


async def test_dm_media_blob_chunked_missing_chunk_bails(inbound_with_media):
    """Final chunk arriving without an earlier part → log + bail.

    The receiver doesn't write a partial concat; the sender's
    outbox retry resends the missing chunk and finalisation runs
    again.
    """
    svc, media_dir, _db = inbound_with_media
    # Skip chunk 0, deliver final (chunk 1 of 2).
    await svc._on_dm_media_blob(
        _event(
            FederationEventType.DM_MEDIA_BLOB,
            {
                "media_blob_id": "m-1",
                "message_id": "m-1",
                "conversation_id": "conv-1",
                "mime_type": "image/webp",
                "chunk_index": 1,
                "chunk_count": 2,
                "final": True,
                "bytes_b64": base64.b64encode(b"tail").decode("ascii"),
            },
        )
    )
    # Final wasn't able to finalise (chunk 0 missing) — the
    # ``m-1.webp`` file should NOT exist yet, the part for index
    # 1 should be there.
    assert (media_dir / "m-1.part00001").is_file()
    assert not (media_dir / "m-1.webp").is_file()


# ── MIME sniff ──────────────────────────────────────────────────────────


async def test_dm_media_blob_mime_sniff_flags_failed(inbound_with_media):
    """A claimed ``image/webp`` whose bytes don't match WebP
    magic still stores the file but flips
    ``media_sync_status='failed'`` so the receiver bubble surfaces
    the warning."""
    svc, media_dir, db = inbound_with_media
    await svc._on_dm_media_blob(
        _event(
            FederationEventType.DM_MEDIA_BLOB,
            {
                "media_blob_id": "m-1",
                "message_id": "m-1",
                "conversation_id": "conv-1",
                "mime_type": "image/webp",
                "bytes_b64": base64.b64encode(b"NOT-A-WEBP-BLOB").decode(),
            },
        )
    )
    dest = media_dir / "m-1.webp"
    assert dest.is_file()
    row = await db.fetchone(
        "SELECT media_sync_status FROM conversation_messages WHERE id=?",
        ("m-1",),
    )
    assert row is not None
    assert row["media_sync_status"] == "failed"


def test_bytes_match_mime_webp_and_webm():
    assert _bytes_match_mime(_WEBP_HEADER, "image/webp") is True
    assert _bytes_match_mime(b"\x1a\x45\xdf\xa3...", "video/webm") is True
    assert _bytes_match_mime(b"random", "image/webp") is False
    assert _bytes_match_mime(b"random", "image/jpeg") is False  # no entry → flag
    # Files / unknown text MIMEs pass through (``None``).
    assert _bytes_match_mime(b"%PDF", "application/pdf") is None
    assert _bytes_match_mime(b"hello", "text/plain") is None


def test_mime_to_ext_known_and_unknown():
    assert _mime_to_ext("image/webp") == ".webp"
    assert _mime_to_ext("application/pdf") == ".pdf"
    assert _mime_to_ext("text/x-unknown") == ".bin"
