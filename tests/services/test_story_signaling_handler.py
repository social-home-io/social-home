"""Tests for :class:`StorySignalingHandler`.

Uses a stub peer factory so the framing + orchestration logic can be
exercised without an :mod:`aiolibdatachannel` runtime. The real
factory is wired in production at ``app._on_startup`` time and is
covered indirectly by the integration suite (PR3 → real-peer).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.story import StoryAudience, StoryFrameType
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.story_repo import SqliteStoryRepo
from socialhome.services import story_public_framing as framing
from socialhome.services.story_signaling_handler import (
    MAX_CONCURRENT_VIEWERS_PER_STORY,
    StorySignalingHandler,
)


# ─── Stub peer ───────────────────────────────────────────────────────────


class _StubPeer:
    """Records every call. ``wait_open`` resolves immediately so the
    handler proceeds straight to streaming; tests inspect ``frames``
    after the serve task drains."""

    def __init__(self):
        self.remote_offer: str | None = None
        self.answer_sdp: str = "v=0\r\no=- ans"
        self.candidates: list[dict] = []
        self.frames: list[bytes] = []
        self.closed = False

    async def set_remote_offer(self, sdp: str) -> None:
        self.remote_offer = sdp

    async def create_answer(self) -> str:
        return self.answer_sdp

    async def add_ice_candidate(self, candidate: dict) -> None:
        self.candidates.append(candidate)

    async def wait_open(self) -> None:
        return None

    async def send(self, frame_bytes: bytes) -> None:
        self.frames.append(frame_bytes)

    async def close(self) -> None:
        self.closed = True


class _StubResp:
    def __init__(self, status: int = 200, body: dict | None = None):
        self.status = status
        self._body = body or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body

    async def text(self):
        return ""


class _StubSession:
    def __init__(self):
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _StubResp(200, {"status": "ok"})


# ─── Fixtures ────────────────────────────────────────────────────────────


def _expires(days: int = 7) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


@pytest.fixture
async def repos(db, tmp_dir):
    media_dir = tmp_dir / "media"
    media_dir.mkdir()
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u1', 'alice', 'Alice', 'active')",
    )
    await db.enqueue(
        """
        INSERT INTO gfs_connections(
            id, gfs_instance_id, display_name, public_key, inbox_url,
            status, paired_at
        ) VALUES('gfs-abc','gfs-abc','GFS','ff','https://gfs.example',
                 'active', datetime('now'))
        """,
    )
    return {
        "stories": SqliteStoryRepo(db),
        "gfs": SqliteGfsConnectionRepo(db),
        "media_dir": str(media_dir),
    }


@pytest.fixture
async def published_story(repos):
    """Story with two image frames, marked as publicly published."""
    story = await repos["stories"].find_or_create_today(
        author_user_id="u1",
        audience_kind=StoryAudience.ALL_PAIRED,
        audience=(),
        story_date="2026-05-03",
        expires_at=_expires(),
    )
    f1 = await repos["stories"].append_frame(
        story_id=story.id,
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/a.jpg",
    )
    f2 = await repos["stories"].append_frame(
        story_id=story.id,
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/b.jpg",
    )
    # Drop the bytes for both frames into the test media dir.
    media_dir = repos["media_dir"]
    (open(f"{media_dir}/a.jpg", "wb")).write(b"A" * 1000)
    (open(f"{media_dir}/b.jpg", "wb")).write(b"B" * 200_000)  # big-ish
    await repos["stories"].mark_published(
        story.id,
        gfs_id="gfs-abc",
        published_at=datetime.now(timezone.utc).isoformat(),
    )
    return story.id, f1.id, f2.id


def _make_handler(repos, *, peer_factory=None):
    last_peer: list[_StubPeer] = []

    def _factory(_ice):
        peer = _StubPeer()
        last_peer.append(peer)
        return peer

    handler = StorySignalingHandler(
        repos["stories"],
        repos["gfs"],
        media_dir=repos["media_dir"],
        peer_factory=peer_factory or _factory,
    )
    handler.attach_session(_StubSession())
    handler.attach_identity(own_instance_id="inst-self", signing_key=b"\x00" * 32)
    handler.attach_ice_servers([{"urls": ["stun:stun.example"]}])
    return handler, last_peer


async def _drain(handler) -> None:
    """Wait for every in-flight serve task to drain."""
    tasks = [s.task for s in handler._sessions.values() if s.task is not None]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ─── Happy path ─────────────────────────────────────────────────────────


async def test_offer_streams_metadata_then_chunks_then_stream_end(
    repos,
    published_story,
):
    story_id, f1, f2 = published_story
    handler, peers = _make_handler(repos)
    await handler.handle_signal(
        {"kind": "offer", "session_id": "s-1", "story_id": story_id, "sdp": "v=0"}
    )
    await _drain(handler)
    assert peers, "peer factory wasn't called"
    peer = peers[0]
    assert peer.remote_offer == "v=0"
    assert peer.closed
    # Frame stream: meta, then 1 chunk for the small file, then 4 chunks
    # for the 200KB file (200000 / 65536 = 3 full + 1 partial), then
    # stream_end.
    decoded = [framing.decode(b) for b in peer.frames]
    kinds = [f.header["kind"] for f in decoded]
    assert kinds[0] == framing.KIND_STORY_META
    assert kinds[-1] == framing.KIND_STREAM_END
    chunks = [f for f in decoded if f.header["kind"] == framing.KIND_FRAME_CHUNK]
    # Both frames represented.
    assert {c.header["frame_id"] for c in chunks} == {f1, f2}
    # The last chunk per frame carries is_last_chunk=True.
    last_per_frame = {}
    for c in chunks:
        if c.header["is_last_chunk"]:
            last_per_frame[c.header["frame_id"]] = c
    assert set(last_per_frame.keys()) == {f1, f2}


async def test_meta_manifest_carries_byte_length_and_content_type(
    repos,
    published_story,
):
    story_id, f1, f2 = published_story
    handler, peers = _make_handler(repos)
    await handler.handle_signal(
        {"kind": "offer", "session_id": "s-1", "story_id": story_id, "sdp": "v=0"}
    )
    await _drain(handler)
    meta = framing.decode(peers[0].frames[0]).header
    by_id = {f["frame_id"]: f for f in meta["frames"]}
    assert by_id[f1]["byte_length"] == 1000
    assert by_id[f1]["content_type"] == "image/jpeg"
    assert by_id[f2]["byte_length"] == 200_000


async def test_handler_posts_signed_answer_to_correct_gfs(repos, published_story):
    story_id, *_ = published_story
    handler, _peers = _make_handler(repos)
    sess = _StubSession()
    handler._http_client = sess  # type: ignore[assignment]
    await handler.handle_signal(
        {"kind": "offer", "session_id": "s-1", "story_id": story_id, "sdp": "v=0"}
    )
    await _drain(handler)
    assert sess.posts
    url, body = sess.posts[0]
    assert url == "https://gfs.example/gfs/story_rtc/answer"
    assert body["session_id"] == "s-1"
    assert body["instance_id"] == "inst-self"
    assert "signature" in body


# ─── ICE plumbing ───────────────────────────────────────────────────────


async def test_ice_frame_is_forwarded_to_peer(repos, published_story):
    story_id, *_ = published_story

    open_event = asyncio.Event()

    class _SlowPeer(_StubPeer):
        async def wait_open(self) -> None:
            # Block until the test signals ready.
            await open_event.wait()

    peers: list[_StubPeer] = []

    def _factory(_ice):
        p = _SlowPeer()
        peers.append(p)
        return p

    handler, _ = _make_handler(repos, peer_factory=_factory)
    await handler.handle_signal(
        {"kind": "offer", "session_id": "s-1", "story_id": story_id, "sdp": "v=0"}
    )
    # Hand off to the loop so the serve task can spin up.
    await asyncio.sleep(0)
    await handler.handle_signal(
        {
            "kind": "ice",
            "session_id": "s-1",
            "candidate": {"candidate": "x", "sdpMid": "0"},
        }
    )
    assert peers[0].candidates == [{"candidate": "x", "sdpMid": "0"}]
    open_event.set()  # let the serve task finish so cleanup is clean
    await _drain(handler)


async def test_ice_for_unknown_session_is_no_op(repos, published_story):
    handler, _ = _make_handler(repos)
    await handler.handle_signal(
        {"kind": "ice", "session_id": "missing", "candidate": {"x": 1}}
    )
    # No peers created, no exceptions.


# ─── Failure paths ──────────────────────────────────────────────────────


async def test_offer_for_unpublished_story_skips_peer(repos):
    """Unknown / unpublished story => no peer is opened, session
    is dropped silently (viewer times out)."""
    handler, peers = _make_handler(repos)
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-x",
            "story_id": "missing",
            "sdp": "v=0",
        }
    )
    await _drain(handler)
    assert peers == []


async def test_per_story_cap_rejects_eleventh_offer(repos, published_story):
    story_id, *_ = published_story

    open_event = asyncio.Event()

    class _SlowPeer(_StubPeer):
        async def wait_open(self) -> None:
            await open_event.wait()

    peers: list[_StubPeer] = []

    def _factory(_ice):
        p = _SlowPeer()
        peers.append(p)
        return p

    handler, _ = _make_handler(repos, peer_factory=_factory)
    for i in range(MAX_CONCURRENT_VIEWERS_PER_STORY):
        await handler.handle_signal(
            {
                "kind": "offer",
                "session_id": f"s-{i}",
                "story_id": story_id,
                "sdp": "v=0",
            }
        )
    # 10 sessions hold open peers.
    assert len(peers) == MAX_CONCURRENT_VIEWERS_PER_STORY

    # The 11th must be rejected — no new peer.
    before = len(peers)
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-overflow",
            "story_id": story_id,
            "sdp": "v=0",
        }
    )
    assert len(peers) == before
    open_event.set()
    await _drain(handler)


async def test_duplicate_session_id_is_dropped(repos, published_story):
    story_id, *_ = published_story

    open_event = asyncio.Event()

    class _SlowPeer(_StubPeer):
        async def wait_open(self) -> None:
            await open_event.wait()

    peers: list[_StubPeer] = []

    def _factory(_ice):
        p = _SlowPeer()
        peers.append(p)
        return p

    handler, _ = _make_handler(repos, peer_factory=_factory)
    await handler.handle_signal(
        {"kind": "offer", "session_id": "s-1", "story_id": story_id, "sdp": "v=0"}
    )
    await handler.handle_signal(
        {"kind": "offer", "session_id": "s-1", "story_id": story_id, "sdp": "v=0"}
    )
    assert len(peers) == 1
    open_event.set()
    await _drain(handler)


# ─── Stop / cleanup ─────────────────────────────────────────────────────


async def test_stop_cancels_in_flight_sessions(repos, published_story):
    story_id, *_ = published_story

    open_event = asyncio.Event()

    class _SlowPeer(_StubPeer):
        async def wait_open(self) -> None:
            await open_event.wait()

    peers: list[_StubPeer] = []

    def _factory(_ice):
        p = _SlowPeer()
        peers.append(p)
        return p

    handler, _ = _make_handler(repos, peer_factory=_factory)
    await handler.handle_signal(
        {"kind": "offer", "session_id": "s-1", "story_id": story_id, "sdp": "v=0"}
    )
    await asyncio.sleep(0)  # allow the serve task to start
    await handler.stop()
    assert peers[0].closed
    assert "s-1" not in handler._sessions
