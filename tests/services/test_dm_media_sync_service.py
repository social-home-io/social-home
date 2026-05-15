"""Tests for the v_3 DM media-sync service.

Covers the two halves of the preview-now-sync-later flow on the
*sender* side:

* :meth:`DmMediaSyncService.build_preview` produces a base64-encoded
  WebP thumbnail for images, ``None`` for video / file kinds.
* :meth:`DmMediaSyncService.enqueue_for_message` writes outbox
  entries via the repo abstraction; :meth:`flush_once` reads them,
  dispatches a ``DM_MEDIA_BLOB`` through the federation service,
  and deletes the row on success.

In-memory fakes for the repos keep the fixture small — the
repo's SQLite implementation is covered by integration tests
elsewhere.
"""

from __future__ import annotations

import base64
import io
import pathlib
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from PIL import Image

from socialhome.domain.conversation import ConversationMessage
from socialhome.domain.federation import FederationEventType
from socialhome.repositories.dm_media_outbox_repo import DmMediaOutboxEntry
from socialhome.services.dm_media_sync_service import DmMediaSyncService


pytestmark = pytest.mark.asyncio


# ── Fakes ──────────────────────────────────────────────────────────────


@dataclass
class _Row:
    """Mutable outbox row used by the in-memory fake."""

    blob_id: str
    message_id: str
    target_instance_id: str
    bytes_path: str
    status: str = "pending"
    attempts: int = 0
    next_attempt_at: str = "2000-01-01 00:00:00"
    last_error: str | None = None


class FakeOutboxRepo:
    """In-memory outbox keyed on ``(blob_id, target_instance_id)``."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], _Row] = {}

    async def enqueue(self, *, blob_id, message_id, target_instance_id, bytes_path):
        key = (blob_id, target_instance_id)
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.rows.setdefault(
            key,
            _Row(
                blob_id=blob_id,
                message_id=message_id,
                target_instance_id=target_instance_id,
                bytes_path=bytes_path,
                next_attempt_at=now_iso,
            ),
        )

    async def list_due(self, *, limit: int = 25):
        # Mirror the SQLite implementation's filter: only rows whose
        # ``next_attempt_at`` is in the past are due. Empty / default
        # timestamps read as "way past" so freshly-enqueued rows
        # surface immediately.
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        return [
            DmMediaOutboxEntry(
                blob_id=r.blob_id,
                message_id=r.message_id,
                target_instance_id=r.target_instance_id,
                bytes_path=r.bytes_path,
                status=r.status,
                attempts=r.attempts,
                next_attempt_at=r.next_attempt_at,
                last_error=r.last_error,
                created_at="",
            )
            for r in self.rows.values()
            if r.status == "pending" and r.next_attempt_at <= now_iso
        ]

    async def mark_in_flight(self, *, blob_id, target_instance_id):
        self.rows[(blob_id, target_instance_id)].status = "in_flight"

    async def delete(self, *, blob_id, target_instance_id):
        self.rows.pop((blob_id, target_instance_id), None)

    async def reschedule(
        self,
        *,
        blob_id,
        target_instance_id,
        attempts,
        next_attempt_at,
        last_error,
    ):
        r = self.rows[(blob_id, target_instance_id)]
        r.status = "pending"
        r.attempts = attempts
        r.next_attempt_at = next_attempt_at
        r.last_error = last_error

    async def mark_failed(
        self,
        *,
        blob_id,
        target_instance_id,
        last_error,
    ):
        r = self.rows[(blob_id, target_instance_id)]
        r.status = "failed"
        r.last_error = last_error

    async def list_for_message(self, message_id):
        return [
            DmMediaOutboxEntry(
                blob_id=r.blob_id,
                message_id=r.message_id,
                target_instance_id=r.target_instance_id,
                bytes_path=r.bytes_path,
                status=r.status,
                attempts=r.attempts,
                next_attempt_at=r.next_attempt_at,
                last_error=r.last_error,
                created_at="",
            )
            for r in self.rows.values()
            if r.message_id == message_id
        ]


class FakeConvosRepo:
    """Just enough conversation_repo for the service's needs."""

    def __init__(self) -> None:
        self.msgs: dict[str, ConversationMessage] = {}
        self.sync_status_updates: list[tuple[str, str | None, str | None]] = []

    async def get_message(self, message_id: str):
        return self.msgs.get(message_id)

    async def update_media_sync_status(self, *, message_id, status, media_url=None):
        self.sync_status_updates.append((message_id, status, media_url))


class FakeFederation:
    """Records every ``send_event`` call."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.should_fail = False

    async def send_event(self, *, to_instance_id, event_type, payload):
        if self.should_fail:
            raise RuntimeError("simulated transport failure")
        self.sent.append(
            {"to": to_instance_id, "event": event_type, "payload": payload},
        )


# ── Helpers ────────────────────────────────────────────────────────────


def _make_test_image(media_dir: pathlib.Path, name: str = "cat.webp") -> pathlib.Path:
    """Write a tiny WebP to ``media_dir`` so the preview path resolves."""
    img = Image.new("RGB", (640, 480), color=(200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=82)
    dest = media_dir / name
    dest.write_bytes(buf.getvalue())
    return dest


@pytest.fixture
def stack(tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    convos = FakeConvosRepo()
    outbox = FakeOutboxRepo()
    fed = FakeFederation()
    svc = DmMediaSyncService(
        convos=convos,
        outbox=outbox,
        federation=fed,
        media_dir=media_dir,
    )
    # Pre-seed one media message so flush_once can read its metadata.
    convos.msgs["m1"] = ConversationMessage(
        id="m1",
        conversation_id="c1",
        sender_user_id="u-alice",
        content="",
        type="image",
        media_url="api/media/cat.webp",
        file_name="cat.jpg",
        mime_type="image/webp",
        file_size_bytes=4321,
        media_blob_id="m1",
        created_at=datetime.now(timezone.utc),
    )
    return {
        "svc": svc,
        "convos": convos,
        "outbox": outbox,
        "fed": fed,
        "media_dir": media_dir,
    }


# ── Preview-build tests ────────────────────────────────────────────────


async def test_build_preview_image_returns_b64(stack):
    """An image source yields a base64-encoded WebP under the cap."""
    _make_test_image(stack["media_dir"])
    out = await stack["svc"].build_preview(
        media_url="api/media/cat.webp",
        kind="image",
        mime_type="image/webp",
    )
    assert out is not None
    decoded = base64.b64decode(out)
    # WebP magic — see ``IMAGE_WEBP_MAGIC`` in media_constraints.
    assert decoded[:4] == b"RIFF"
    assert b"WEBP" in decoded[:16]
    # Preview should be well under 50 KB.
    assert len(decoded) < 50_000


async def test_build_preview_video_extracts_first_frame(stack, tmp_path):
    """Video preview pulls the first frame and downscales to the cap.

    We synthesise a tiny single-frame WebM via PyAV so the test
    doesn't depend on a real video file shipped under tests/. The
    output should be a WebP under the inline-envelope budget so the
    receiver can render a recognisable poster immediately.
    """
    import av
    import io

    # 3 frames of 320×240 solid colour @ 25 fps — that's about 0.12 s
    # of footage, plenty for ``generate_thumbnail`` to land the first
    # frame. VideoProcessor only needs to decode one frame.
    buf = io.BytesIO()
    out_container = av.open(buf, mode="w", format="webm")
    stream = out_container.add_stream("vp9", rate=25)
    stream.width = 320
    stream.height = 240
    stream.pix_fmt = "yuv420p"
    from PIL import Image as _Image

    for _ in range(3):
        frame = av.VideoFrame.from_image(
            _Image.new("RGB", (320, 240), color=(50, 100, 200)),
        )
        for packet in stream.encode(frame):
            out_container.mux(packet)
    for packet in stream.encode():
        out_container.mux(packet)
    out_container.close()
    video_bytes = buf.getvalue()
    assert video_bytes, "synthesised video should produce bytes"
    (stack["media_dir"] / "clip.webm").write_bytes(video_bytes)

    out = await stack["svc"].build_preview(
        media_url="api/media/clip.webm",
        kind="video",
        mime_type="video/webm",
    )
    assert out is not None
    decoded = base64.b64decode(out)
    # Output is a WebP (poster encoded by ImageProcessor's
    # downscale path); under the preview-envelope budget.
    assert decoded[:4] == b"RIFF"
    assert b"WEBP" in decoded[:16]
    assert len(decoded) < 50_000


async def test_build_preview_video_undecodable_returns_none(stack):
    """A corrupt video file falls back to ``None``.

    PyAV raises on unparseable bytes; the build_preview branch
    catches and returns ``None`` so the receiver shows the
    play-glyph placeholder instead of the bubble going empty.
    """
    (stack["media_dir"] / "broken.webm").write_bytes(b"not a video")
    out = await stack["svc"].build_preview(
        media_url="api/media/broken.webm",
        kind="video",
        mime_type="video/webm",
    )
    assert out is None


async def test_build_preview_file_returns_none(stack):
    """Files don't carry an inline thumbnail — receiver shows a glyph."""
    (stack["media_dir"] / "invoice.pdf").write_bytes(b"%PDF-1.4 ...")
    out = await stack["svc"].build_preview(
        media_url="api/media/invoice.pdf",
        kind="file",
        mime_type="application/pdf",
    )
    assert out is None


async def test_build_preview_missing_file_returns_none(stack):
    """Vanished source files don't crash."""
    out = await stack["svc"].build_preview(
        media_url="api/media/never_existed.webp",
        kind="image",
        mime_type="image/webp",
    )
    assert out is None


async def test_build_preview_rejects_path_traversal(stack):
    """Malicious ``media_url`` can't escape the media root."""
    out = await stack["svc"].build_preview(
        media_url="api/media/../../etc/passwd",
        kind="image",
        mime_type="image/jpeg",
    )
    assert out is None


# ── Outbox flush tests ─────────────────────────────────────────────────


async def test_enqueue_writes_one_row_per_target(stack):
    _make_test_image(stack["media_dir"])
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="api/media/cat.webp",
        target_instance_ids=["inst-bob", "inst-carol"],
    )
    rows = await stack["outbox"].list_due()
    targets = sorted(r.target_instance_id for r in rows)
    assert targets == ["inst-bob", "inst-carol"]


async def test_flush_once_dispatches_and_deletes(stack):
    _make_test_image(stack["media_dir"])
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="api/media/cat.webp",
        target_instance_ids=["inst-bob"],
    )
    shipped = await stack["svc"].flush_once()
    assert shipped == 1
    sent = stack["fed"].sent
    assert len(sent) == 1
    assert sent[0]["to"] == "inst-bob"
    assert sent[0]["event"] == FederationEventType.DM_MEDIA_BLOB
    payload = sent[0]["payload"]
    assert payload["media_blob_id"] == "m1"
    assert payload["message_id"] == "m1"
    assert payload["file_name"] == "cat.jpg"
    assert payload["mime_type"] == "image/webp"
    assert "bytes_b64" in payload
    # Outbox is empty after delete.
    rows = await stack["outbox"].list_due()
    assert rows == []


async def test_flush_once_reschedules_on_failure(stack):
    _make_test_image(stack["media_dir"])
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="api/media/cat.webp",
        target_instance_ids=["inst-bob"],
    )
    stack["fed"].should_fail = True
    shipped = await stack["svc"].flush_once()
    assert shipped == 0
    # No rows due-now (reschedule pushed the timestamp out).
    immediate = await stack["outbox"].list_due()
    assert immediate == []
    all_rows = await stack["outbox"].list_for_message("m1")
    assert len(all_rows) == 1
    assert all_rows[0].attempts == 1
    assert all_rows[0].status == "pending"
    assert all_rows[0].last_error is not None


async def test_flush_once_chunks_large_files(stack):
    """A file above ``SINGLE_CHUNK_BYTES_THRESHOLD`` ships as N chunks.

    Every chunk lands as its own ``DM_MEDIA_BLOB`` envelope with a
    monotonic ``chunk_index`` and the same ``chunk_count``. Only
    the last chunk carries ``final=true``.
    """
    from socialhome.services.dm_media_sync_service import (
        MAX_BLOB_CHUNK_BYTES,
        SINGLE_CHUNK_BYTES_THRESHOLD,
    )

    # Write a file just over the single-chunk threshold so the
    # split lands at 5 chunks (4 full + 1 short).
    target_size = SINGLE_CHUNK_BYTES_THRESHOLD + 4 * MAX_BLOB_CHUNK_BYTES
    big = stack["media_dir"] / "big.bin"
    big.write_bytes(b"x" * target_size)
    # Seed a matching message row so flush_once can read metadata.
    stack["convos"].msgs["m-big"] = ConversationMessage(
        id="m-big",
        conversation_id="c1",
        sender_user_id="u-alice",
        content="",
        type="file",
        media_url="api/media/big.bin",
        file_name="big.bin",
        mime_type="application/octet-stream",
        file_size_bytes=target_size,
        media_blob_id="m-big",
        created_at=datetime.now(timezone.utc),
    )
    await stack["svc"].enqueue_for_message(
        message_id="m-big",
        media_url="api/media/big.bin",
        target_instance_ids=["inst-bob"],
    )
    shipped = await stack["svc"].flush_once()
    assert shipped == 1

    sent = stack["fed"].sent
    assert len(sent) >= 2, "large file must split into multiple chunks"
    # Every send went to the same peer and shared the same
    # blob_id + chunk_count.
    chunk_count_seen = {s["payload"]["chunk_count"] for s in sent}
    assert chunk_count_seen == {len(sent)}, (
        f"chunk_count mismatch: {chunk_count_seen} vs N={len(sent)}"
    )
    indices = [s["payload"]["chunk_index"] for s in sent]
    assert indices == list(range(len(sent))), "chunks must dispatch in order"
    # Only the last chunk is ``final=true``.
    assert all(s["payload"]["final"] is False for s in sent[:-1])
    assert sent[-1]["payload"]["final"] is True
    # And the row was deleted after the full batch landed.
    rows = await stack["outbox"].list_for_message("m-big")
    assert rows == []


async def test_flush_once_single_chunk_keeps_legacy_shape(stack):
    """Files under the threshold ride one envelope (back-compat).

    The chunk-metadata fields are still present but read as a
    single-chunk transfer — receivers on older builds that don't
    inspect them treat the payload as a complete file.
    """
    _make_test_image(stack["media_dir"])
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="api/media/cat.webp",
        target_instance_ids=["inst-bob"],
    )
    await stack["svc"].flush_once()
    sent = stack["fed"].sent
    assert len(sent) == 1
    assert sent[0]["payload"]["chunk_index"] == 0
    assert sent[0]["payload"]["chunk_count"] == 1
    assert sent[0]["payload"]["final"] is True
