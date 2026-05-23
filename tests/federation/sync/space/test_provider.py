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
