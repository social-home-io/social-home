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


# ── _receive_media_preview branches ───────────────────────────────────


async def test_receive_media_preview_non_media_passes_through(inbound_with_media):
    """Text / transcript / location messages flow through unchanged."""
    svc, _media_dir, _db = inbound_with_media
    url, status = await svc._receive_media_preview(
        payload={"media_url": "https://elsewhere/foo.jpg"},
        message_id="m-1",
        msg_type="text",
    )
    assert url == "https://elsewhere/foo.jpg"
    assert status is None


async def test_receive_media_preview_pre_arrived_full_file(inbound_with_media):
    """If the blob landed before the message, the full file is
    adopted directly — no preview write, no pending state."""
    svc, media_dir, _db = inbound_with_media
    full = media_dir / "m-2.webp"
    full.write_bytes(_WEBP_HEADER)
    url, status = await svc._receive_media_preview(
        payload={
            "media_blob_id": "m-2",
            "mime_type": "image/webp",
            # preview shouldn't be touched because full file exists
            "preview_bytes_b64": "ignored",
        },
        message_id="m-2",
        msg_type="image",
    )
    assert url == "api/media/m-2.webp"
    assert status is None
    # Preview file was dropped (if it existed from a stale attempt).
    assert not (media_dir / "m-2.preview.webp").exists()


async def test_receive_media_preview_writes_preview(inbound_with_media):
    """A normal v_3 media DM with embedded preview lands as a
    ``<msg_id>.preview.webp`` and the row stays ``pending``."""
    svc, media_dir, _db = inbound_with_media
    url, status = await svc._receive_media_preview(
        payload={
            "media_blob_id": "m-3",
            "mime_type": "image/webp",
            "preview_bytes_b64": base64.b64encode(_WEBP_HEADER).decode(),
        },
        message_id="m-3",
        msg_type="image",
    )
    assert url == "api/media/m-3.preview.webp"
    assert status == "pending"
    assert (media_dir / "m-3.preview.webp").is_file()


async def test_receive_media_preview_without_preview_field(inbound_with_media):
    """v_3 media without an inline preview (video / file before
    poster-extraction landed, or a sender that built none) sets
    pending state and ``media_url=None`` — the SPA shows a
    placeholder glyph until the blob arrives."""
    svc, _media_dir, _db = inbound_with_media
    url, status = await svc._receive_media_preview(
        payload={
            "media_blob_id": "m-4",
            "mime_type": "video/webm",
        },
        message_id="m-4",
        msg_type="video",
    )
    assert url is None
    assert status == "pending"


async def test_receive_media_preview_no_media_dir_returns_pending(db, bus):
    """Service without ``media_dir`` can't write the preview — it
    returns ``pending`` with no URL so the SPA still shows the
    placeholder."""
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        media_dir=None,
    )
    url, status = await svc._receive_media_preview(
        payload={
            "media_blob_id": "m-5",
            "mime_type": "image/webp",
            "preview_bytes_b64": "x",
        },
        message_id="m-5",
        msg_type="image",
    )
    assert url is None
    assert status == "pending"


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


async def test_dm_media_blob_malformed_chunk_meta_defaults_to_single(
    inbound_with_media,
):
    """Garbage ``chunk_index`` / ``chunk_count`` values fall back
    to single-chunk handling so a sender on a different language
    runtime can't poison the receiver."""
    svc, media_dir, db = inbound_with_media
    await svc._on_dm_media_blob(
        _event(
            FederationEventType.DM_MEDIA_BLOB,
            {
                "media_blob_id": "m-1",
                "message_id": "m-1",
                "conversation_id": "conv-1",
                "mime_type": "image/webp",
                "chunk_index": "not-a-number",
                "chunk_count": None,
                "bytes_b64": base64.b64encode(_WEBP_HEADER).decode("ascii"),
            },
        )
    )
    # Garbage chunk fields → defaults to single chunk → file lands
    # at the final destination.
    assert (media_dir / "m-1.webp").is_file()


async def test_dm_media_blob_unknown_mime_uses_bin_ext(inbound_with_media):
    """An unknown ``mime_type`` writes the file with the ``.bin``
    fallback extension."""
    svc, media_dir, _db = inbound_with_media
    await svc._on_dm_media_blob(
        _event(
            FederationEventType.DM_MEDIA_BLOB,
            {
                "media_blob_id": "m-1",
                "message_id": "m-1",
                "conversation_id": "conv-1",
                "mime_type": "application/x-weird",
                "bytes_b64": base64.b64encode(b"weird bytes").decode("ascii"),
            },
        )
    )
    assert (media_dir / "m-1.bin").is_file()


async def test_dm_media_blob_malformed_b64_drops(inbound_with_media):
    """Bad base64 → log + drop, no crash."""
    svc, media_dir, _db = inbound_with_media
    await svc._on_dm_media_blob(
        _event(
            FederationEventType.DM_MEDIA_BLOB,
            {
                "media_blob_id": "m-1",
                "message_id": "m-1",
                "conversation_id": "conv-1",
                "mime_type": "image/webp",
                "bytes_b64": "!@#$%^&*not-valid-b64",
            },
        )
    )
    # No file created.
    assert not (media_dir / "m-1.webp").exists()


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
