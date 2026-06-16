"""Unit tests for :class:`SpaceSyncService`."""

from __future__ import annotations

from typing import Any

import orjson
import pytest

from socialhome.crypto import generate_identity_keypair
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.sync.space.exporter import (
    ChunkBuilder,
    SENTINEL_RESOURCE,
)
from socialhome.federation.sync.space.provider import SpaceSyncService


class _FakeExporter:
    def __init__(self, resource: str, records: list[dict]) -> None:
        self.resource = resource
        self._records = records

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        return list(self._records)


class _FakeCrypto:
    async def encrypt_chunk(self, *, space_id, sync_id, plaintext):
        import base64

        return 0, base64.urlsafe_b64encode(plaintext).decode("ascii")


class _FakeRtc:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_chunk(self, data):
        self.sent.append(data if isinstance(data, bytes) else data.encode())


class _FakeSession:
    def __init__(self, sync_id="sync-x", space_id="sp-1", requester="peer-r"):
        self.sync_id = sync_id
        self.space_id = space_id
        self.requester_instance_id = requester
        self.rtc = _FakeRtc()


@pytest.fixture
def encoder():
    return FederationEncoder(generate_identity_keypair().private_key)


@pytest.fixture
def provider(encoder):
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    exporters = {
        "posts": _FakeExporter("posts", [{"id": "p-1", "author": "u-1"}]),
        "members": _FakeExporter("members", [{"user_id": "u-1", "role": "member"}]),
    }
    return SpaceSyncService(builder=builder, exporters=exporters, sig_suite="ed25519")


async def test_stream_initial_sends_chunks_then_sentinel(provider):
    session = _FakeSession()
    await provider.stream_initial(session)
    # Expect chunks for the two configured exporters + a sentinel.
    assert len(session.rtc.sent) >= 3
    # Parse the last frame — should be the sentinel.
    last = orjson.loads(session.rtc.sent[-1])
    assert last["resource"] == SENTINEL_RESOURCE
    assert last["is_last"] is True


async def test_stream_initial_skips_missing_exporters(provider):
    """Resources without a registered exporter are skipped (this
    fixture only provides 2 of the 11)."""
    session = _FakeSession()
    await provider.stream_initial(session)
    # Chunks are posts + members + sentinel = 3 frames.
    assert len(session.rtc.sent) == 3


async def test_stream_request_more_only_sends_that_resource(provider):
    session = _FakeSession()
    await provider.stream_request_more(session, {"resource": "posts"})
    assert len(session.rtc.sent) == 1
    parsed = orjson.loads(session.rtc.sent[0])
    assert parsed["resource"] == "posts"


async def test_stream_request_more_unknown_resource_is_noop(provider):
    session = _FakeSession()
    await provider.stream_request_more(session, {"resource": "not_real"})
    assert session.rtc.sent == []


# ─── Part C: HTTPS-mode transport ─────────────────────────────────────


async def test_stream_initial_uses_https_when_session_marked(provider):
    """When ``session.transport_mode == "https"`` (the WebRTC handshake
    couldn't finish — carrier-grade NAT, missing STUN — and the
    requester re-issued the BEGIN with ``prefer_direct=False``), the
    provider MUST ship chunks as ``SPACE_SYNC_CHUNK`` federation
    events through the attached federation service instead of trying
    the absent DataChannel. Before this fix relay-mode sync accepted
    the session and then did nothing.

    The send rides ``send_with_mesh_fallback`` (not bare ``send_event``)
    so a mesh-only requester — a member that joined over a MESH route
    and isn't a confirmed direct peer — receives the chunks via
    ``SPACE_ROUTED``; a confirmed peer still gets the direct path
    internally."""
    from unittest.mock import AsyncMock

    session = _FakeSession()
    session.rtc = None
    session.transport_mode = "https"

    federation = AsyncMock()
    provider.attach_federation(federation)

    await provider.stream_initial(session)

    # Every chunk + the sentinel travels via the mesh-fallback path.
    assert federation.send_with_mesh_fallback.await_count >= 3
    first_call = federation.send_with_mesh_fallback.await_args_list[0]
    assert first_call.kwargs["event_type"].value == "space_sync_chunk"
    assert first_call.kwargs["to_instance_id"] == session.requester_instance_id
    assert first_call.kwargs["space_id"] == session.space_id
    assert first_call.kwargs["payload"]["sync_id"] == session.sync_id
    assert first_call.kwargs["payload"]["chunk"]  # serialised chunk body


async def test_stream_initial_https_requires_attached_federation(provider):
    """HTTPS-mode without ``attach_federation`` is a wiring bug; the
    provider raises so the missing wiring shows up in tests instead
    of silently dropping chunks in production."""
    session = _FakeSession()
    session.rtc = None
    session.transport_mode = "https"
    with pytest.raises(RuntimeError, match="attach_federation"):
        await provider._send(session, {"resource": "posts"})


# ─── Catch-up media (#PR442) ──────────────────────────────────────────


async def test_stream_initial_enqueues_catchup_media(encoder):
    """After the metadata sentinel, the provider must also enqueue
    space_media_outbox rows for every referenced post + gallery
    media URL, targeted at the requesting peer. Without this a
    new member sees the post / gallery rows but the images stay
    broken until someone uploads NEW media."""
    from unittest.mock import AsyncMock
    from dataclasses import dataclass

    @dataclass
    class _Post:
        id: str
        media_url: str | None = None
        image_urls: tuple = ()
        file_meta: object = None

    @dataclass
    class _GalleryItem:
        id: str
        url: str
        thumbnail_url: str

    class _PostRepo:
        async def list_feed(self, space_id, limit=1000):
            return [
                _Post(id="post-1", image_urls=("api/media/a.webp",)),
                _Post(id="post-2", media_url="api/media/v.webm"),
            ]

    @dataclass
    class _GalleryAlbum:
        id: str

    class _GalleryRepo:
        async def list_albums(self, space_id, *, limit=200):
            return [_GalleryAlbum(id="album-1")]

        async def list_items(self, album_id, *, limit=500):
            return [
                _GalleryItem(
                    id="g-1",
                    url="api/media/full.webp",
                    thumbnail_url="api/media/thumb.webp",
                ),
            ]

    media_sync = AsyncMock()
    media_sync.enqueue_for_blob = AsyncMock()
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    svc = SpaceSyncService(
        builder=builder,
        exporters={},  # the catch-up step doesn't depend on exporters
        sig_suite="ed25519",
        media_sync=media_sync,
        space_post_repo=_PostRepo(),
        gallery_repo=_GalleryRepo(),
    )
    session = _FakeSession(requester="peer-newcomer")
    await svc.stream_initial(session)

    # One enqueue per post + one per gallery item.
    assert media_sync.enqueue_for_blob.await_count == 3
    calls = media_sync.enqueue_for_blob.call_args_list
    by_correlation = {c.kwargs["correlation_id"]: c.kwargs for c in calls}
    assert by_correlation["post-1"]["media_urls"] == ["api/media/a.webp"]
    assert by_correlation["post-2"]["media_urls"] == ["api/media/v.webm"]
    assert set(by_correlation["g-1"]["media_urls"]) == {
        "api/media/full.webp",
        "api/media/thumb.webp",
    }
    # All targeted at the newcomer only — not a broadcast.
    for c in calls:
        assert c.kwargs["target_instance_ids"] == ["peer-newcomer"]


async def test_stream_initial_no_media_sync_wired_is_noop(encoder):
    """Without ``media_sync`` (test stacks), the catch-up step is a
    no-op — metadata sync still finishes."""
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    svc = SpaceSyncService(
        builder=builder,
        exporters={"posts": _FakeExporter("posts", [])},
        sig_suite="ed25519",
        media_sync=None,
    )
    session = _FakeSession()
    await svc.stream_initial(session)
    # Sentinel still landed.
    parsed = orjson.loads(session.rtc.sent[-1])
    assert parsed["resource"] == SENTINEL_RESOURCE


# ─── Bazaar catch-up (#PR445) ─────────────────────────────────────────


async def test_stream_initial_enqueues_bazaar_catchup_media(encoder):
    """Catch-up enumerates bazaar listings and ships their image bytes
    too — the wrapper Post's ``image_urls`` is always empty for
    ``PostType.BAZAAR``, the photos live on ``BazaarListing.image_urls``.
    """
    from unittest.mock import AsyncMock
    from dataclasses import dataclass

    @dataclass
    class _Listing:
        post_id: str
        image_urls: tuple

    class _BazaarRepo:
        async def list_in_space(self, space_id, *, limit=500):
            return [
                _Listing(
                    post_id="bzr-1",
                    image_urls=("api/media/chair-1.webp", "api/media/chair-2.webp"),
                ),
                _Listing(  # No photos — skipped silently.
                    post_id="bzr-2",
                    image_urls=(),
                ),
            ]

    media_sync = AsyncMock()
    media_sync.enqueue_for_blob = AsyncMock()
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    svc = SpaceSyncService(
        builder=builder,
        exporters={},
        sig_suite="ed25519",
        media_sync=media_sync,
        bazaar_repo=_BazaarRepo(),
    )
    session = _FakeSession(requester="peer-bzr")
    await svc.stream_initial(session)

    # Only the photo-bearing listing got an enqueue.
    media_sync.enqueue_for_blob.assert_awaited_once()
    kw = media_sync.enqueue_for_blob.await_args.kwargs
    assert kw["correlation_id"] == "bzr-1"
    assert kw["target_instance_ids"] == ["peer-bzr"]
    assert list(kw["media_urls"]) == [
        "api/media/chair-1.webp",
        "api/media/chair-2.webp",
    ]


async def test_stream_initial_no_bazaar_repo_is_noop(encoder):
    """Without a ``bazaar_repo`` (e.g. older deployments), the catch-up
    skips the bazaar walk — post + gallery still flow."""
    from unittest.mock import AsyncMock

    media_sync = AsyncMock()
    media_sync.enqueue_for_blob = AsyncMock()
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    svc = SpaceSyncService(
        builder=builder,
        exporters={},
        sig_suite="ed25519",
        media_sync=media_sync,
        bazaar_repo=None,
    )
    session = _FakeSession()
    await svc.stream_initial(session)
    # No bazaar enqueues — no other repos wired either.
    media_sync.enqueue_for_blob.assert_not_awaited()


# ─── Integration: real Sqlite repos (#PR444) ──────────────────────────
#
# Mock-shaped unit tests caught the wire shape but missed that
# ``list_items_for_space`` / ``list_items_for_album`` were never
# actual gallery-repo methods (the test fakes happily defined them).
# This integration test wires ``SpaceSyncService`` to the REAL
# Sqlite repos so a future API rename surfaces as a test failure
# rather than a silent runtime skip.


async def test_stream_initial_catchup_real_repos(tmp_dir, encoder):
    """Catch-up walks the real Sqlite post + gallery repos and enqueues
    bytes for every referenced media URL."""
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock

    from socialhome.db.database import AsyncDatabase
    from socialhome.domain.gallery import GalleryAlbum, GalleryItem
    from socialhome.domain.post import Post, PostType
    from socialhome.repositories.gallery_repo import SqliteGalleryRepo
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo

    db = AsyncDatabase(tmp_dir / "catchup.db", batch_timeout_ms=10)
    await db.startup()
    try:
        # Seed a user + space so FKs on gallery_items.uploaded_by AND
        # space_posts.space_id resolve.
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            ("host", "u-host", "Host"),
        )
        await db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, "
            "owner_username, identity_public_key) VALUES(?,?,?,?,?)",
            ("sp-int", "Int Space", "inst-host", "host", "00" * 32),
        )
        post_repo = SqliteSpacePostRepo(db)
        gallery_repo = SqliteGalleryRepo(db)

        # Post with image_urls — what a feed photo upload produces.
        p1 = Post(
            id="p-int-1",
            author="u-host",
            type=PostType.IMAGE,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            image_urls=("api/media/integration.webp",),
        )
        await post_repo.save("sp-int", p1)

        # Gallery album + one item. ``owner_user_id`` is NULL — the
        # users FK would need a real row otherwise and this test only
        # exercises the sync path, not user provisioning.
        album = GalleryAlbum(
            id="alb-int",
            space_id="sp-int",
            owner_user_id=None,
            name="Integration",
        )
        await gallery_repo.create_album(album)
        item = GalleryItem(
            id="g-int-1",
            album_id="alb-int",
            uploaded_by="u-host",
            item_type="photo",
            url="api/media/integration-full.webp",
            thumbnail_url="api/media/integration-thumb.webp",
            width=32,
            height=32,
        )
        await gallery_repo.create_item(item)

        media_sync = AsyncMock()
        media_sync.enqueue_for_blob = AsyncMock()
        builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
        svc = SpaceSyncService(
            builder=builder,
            exporters={},
            sig_suite="ed25519",
            media_sync=media_sync,
            space_post_repo=post_repo,
            gallery_repo=gallery_repo,
        )
        session = _FakeSession(space_id="sp-int", requester="peer-int")
        await svc.stream_initial(session)

        # Post enqueue: image_urls flow through with id as correlation_id.
        calls = media_sync.enqueue_for_blob.call_args_list
        by_correlation = {c.kwargs["correlation_id"]: c.kwargs for c in calls}
        assert "p-int-1" in by_correlation, (
            f"post catch-up didn't fire — calls: {calls}"
        )
        assert by_correlation["p-int-1"]["media_urls"] == [
            "api/media/integration.webp",
        ]
        # Gallery enqueue: thumb + full both shipped under item id.
        assert "g-int-1" in by_correlation, (
            f"gallery catch-up didn't fire (the bug #443 fixed) — calls: {calls}"
        )
        assert set(by_correlation["g-int-1"]["media_urls"]) == {
            "api/media/integration-thumb.webp",
            "api/media/integration-full.webp",
        }
        # All targeted at the requester only.
        for c in calls:
            assert c.kwargs["target_instance_ids"] == ["peer-int"]
    finally:
        await db.shutdown()


async def test_stream_initial_catchup_logic_bug_propagates(encoder, caplog):
    """A renamed / missing repo method is a programming bug, not an
    operational one. The narrowed ``except sqlite3.Error`` in the
    catch-up path lets ``AttributeError`` propagate to ``stream_initial``'s
    outer handler — the failure surfaces as a single visible log entry
    instead of being silently swallowed per-call (which is how the
    #443 bug landed in main).
    """
    import logging
    from unittest.mock import AsyncMock

    class _BrokenPostRepo:
        # No ``list_feed`` — simulates the API drift the #443 bug exhibited.
        pass

    media_sync = AsyncMock()
    media_sync.enqueue_for_blob = AsyncMock()
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    svc = SpaceSyncService(
        builder=builder,
        exporters={},
        sig_suite="ed25519",
        media_sync=media_sync,
        space_post_repo=_BrokenPostRepo(),
    )
    session = _FakeSession(requester="peer-x")
    with caplog.at_level(logging.ERROR):
        # The outer ``except Exception`` in stream_initial still logs +
        # swallows so a buggy catch-up never tears down the whole sync,
        # but the message comes from the OUTER handler (single visible
        # entry per session) rather than the per-call masks that hid
        # the bug in #443.
        await svc.stream_initial(session)
    assert any("stream_initial failed" in r.getMessage() for r in caplog.records), (
        f"outer handler didn't log the propagated AttributeError: {caplog.records}"
    )
    # And — critically — no enqueue happened, because the bug short-
    # circuited before any successful walk.
    assert media_sync.enqueue_for_blob.await_count == 0
