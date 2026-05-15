"""Tests for voice-note (``type='audio'``) handling in ``DmService``.

Covers the SPA-facing surface: ``send_message(type='audio')`` persists
a row with empty ``content`` (the transcript pending), publishes
``DmMessageCreated``, schedules the background transcription task,
and on success patches the row + publishes ``DmMessageUpdated``.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import DmMessageCreated, DmMessageUpdated
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.dm_service import DmService
from socialhome.services.user_service import UserService


class _FakeTranscription:
    """Stand-in for :class:`AudioTranscriptionService`.

    Drives the deterministic outcome of the fire-and-forget task —
    returning ``result`` (which may be ``None`` to model the
    fail-silent path) when called. Records every invocation so tests
    can assert the call shape.
    """

    def __init__(self, result: str | None) -> None:
        self.result = result
        self.calls: list[tuple[int, str]] = []

    async def transcribe(
        self, audio_bytes: bytes, *, language: str = "en"
    ) -> str | None:
        self.calls.append((len(audio_bytes), language))
        return self.result


@pytest.fixture
async def stack(tmp_dir):
    """Service stack + a pair of provisioned local users + a DM."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key, "
        "identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    events: list = []
    bus.subscribe(DmMessageCreated, events.append)
    bus.subscribe(DmMessageUpdated, events.append)
    user_repo = SqliteUserRepo(db)
    conv_repo = SqliteConversationRepo(db)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)

    media_dir = tmp_dir / "media"
    media_dir.mkdir()
    blob_name = "voice-note.ogg"
    blob_path = media_dir / blob_name
    blob_path.write_bytes(
        b"OggS" + b"\x00" * 64
    )  # bytes shape doesn't matter for the fake

    class Holder:
        transcription = _FakeTranscription("hello world")

    holder = Holder()

    dm_svc = DmService(
        conv_repo,
        user_repo,
        bus,
        audio_transcription=holder.transcription,
        media_dir=pathlib.Path(media_dir),
    )

    anna = await user_svc.provision(username="anna", display_name="Anna")
    bob = await user_svc.provision(username="bob", display_name="Bob")
    dm = await dm_svc.create_dm(creator_username="anna", other_username="bob")

    class S:
        pass

    s = S()
    s.db = db
    s.bus = bus
    s.events = events
    s.dm = dm
    s.anna = anna
    s.bob = bob
    s.dm_svc = dm_svc
    s.transcription = holder
    s.blob_name = blob_name
    s.blob_path = blob_path
    yield s
    await db.shutdown()


# ── Tests ──────────────────────────────────────────────────────────────


async def test_audio_send_persists_with_empty_content(stack):
    """``send_message(type='audio')`` persists the row with empty content."""
    stack.transcription.transcription = _FakeTranscription(None)  # transcript pending
    msg = await stack.dm_svc.send_message(
        stack.dm.id,
        sender_username="anna",
        content="",
        type="audio",
        media_url=f"api/media/{stack.blob_name}",
        file_name=stack.blob_name,
        mime_type="audio/ogg",
        file_size_bytes=128,
    )
    assert msg.type == "audio"
    assert msg.content == ""
    assert msg.mime_type == "audio/ogg"


async def test_audio_send_publishes_dm_message_created(stack):
    """The created event mirrors the audio metadata."""
    await stack.dm_svc.send_message(
        stack.dm.id,
        sender_username="anna",
        content="",
        type="audio",
        media_url=f"api/media/{stack.blob_name}",
        file_name=stack.blob_name,
        mime_type="audio/ogg",
        file_size_bytes=128,
    )
    created = [e for e in stack.events if isinstance(e, DmMessageCreated)]
    assert len(created) == 1
    assert created[0].message_type == "audio"
    assert created[0].mime_type == "audio/ogg"


async def test_audio_send_rejects_invalid_type_string(stack):
    """A typo'd type still goes through the MESSAGE_TYPES gate."""
    with pytest.raises(ValueError):
        await stack.dm_svc.send_message(
            stack.dm.id,
            sender_username="anna",
            content="",
            type="audioo",
            media_url=f"api/media/{stack.blob_name}",
        )


async def test_audio_send_rejects_empty_media_url(stack):
    """Voice notes inherit the media-required rule from images / videos."""
    with pytest.raises(ValueError):
        await stack.dm_svc.send_message(
            stack.dm.id,
            sender_username="anna",
            content="",
            type="audio",
            media_url=None,
        )


async def test_audio_send_schedules_transcription_task(stack):
    """The background task runs and fires ``DmMessageUpdated`` on success."""
    await stack.dm_svc.send_message(
        stack.dm.id,
        sender_username="anna",
        content="",
        type="audio",
        media_url=f"api/media/{stack.blob_name}",
        file_name=stack.blob_name,
        mime_type="audio/ogg",
        file_size_bytes=128,
    )
    # The fire-and-forget transcribe-and-patch task lives on the
    # service. Drain it before asserting — tests can't observe an
    # awaited-from-nowhere coroutine otherwise.
    for task in list(stack.dm_svc._pending_transcribe_tasks):
        await task

    # The fake transcription returned "hello world" — the row was
    # patched and ``DmMessageUpdated`` fired with the same text.
    updated = [e for e in stack.events if isinstance(e, DmMessageUpdated)]
    assert len(updated) == 1
    assert updated[0].content == "hello world"
    assert updated[0].conversation_id == stack.dm.id


async def test_audio_send_skips_transcription_when_service_unavailable(stack):
    """No transcription service ⇒ no DmMessageUpdated, audio still sends."""
    # Rebuild the service without the audio transcription dep.
    bare_dm = DmService(
        stack.dm_svc._convos,
        stack.dm_svc._users,
        stack.bus,
        audio_transcription=None,
        media_dir=None,
    )
    await bare_dm.send_message(
        stack.dm.id,
        sender_username="anna",
        content="",
        type="audio",
        media_url=f"api/media/{stack.blob_name}",
        file_name=stack.blob_name,
        mime_type="audio/ogg",
        file_size_bytes=128,
    )
    # Allow any other scheduled work to drain before asserting.
    await asyncio.sleep(0)
    assert not [e for e in stack.events if isinstance(e, DmMessageUpdated)]


async def test_audio_send_transcription_returns_none_no_update(stack):
    """Fail-silent transcription: row stays empty, no update event."""
    stack.dm_svc._audio_transcription = _FakeTranscription(None)
    await stack.dm_svc.send_message(
        stack.dm.id,
        sender_username="anna",
        content="",
        type="audio",
        media_url=f"api/media/{stack.blob_name}",
        file_name=stack.blob_name,
        mime_type="audio/ogg",
        file_size_bytes=128,
    )
    for task in list(stack.dm_svc._pending_transcribe_tasks):
        await task
    assert not [e for e in stack.events if isinstance(e, DmMessageUpdated)]


async def test_audio_transcription_handles_missing_blob(stack):
    """Missing blob doesn't crash the task — the row just stays empty."""
    await stack.dm_svc.send_message(
        stack.dm.id,
        sender_username="anna",
        content="",
        type="audio",
        media_url="api/media/missing.ogg",
        file_name="missing.ogg",
        mime_type="audio/ogg",
        file_size_bytes=128,
    )
    for task in list(stack.dm_svc._pending_transcribe_tasks):
        await task
    assert not [e for e in stack.events if isinstance(e, DmMessageUpdated)]
