"""Author-side signalling + DataChannel streamer for §stories_public.

Receives ``{type:"story_signal", ...}`` frames from
:class:`GfsWsClient` (the existing SH↔GFS WebSocket). On a fresh
``offer`` it spins up an :mod:`aiolibdatachannel` answerer
PeerConnection, posts the SDP answer back to the GFS via the signed
``/gfs/story_rtc/answer`` endpoint, applies remote ICE candidates as
they arrive, and once the DataChannel opens it streams the story
metadata + frame bytes using the framing protocol in
:mod:`socialhome.services.story_public_framing`.

The PeerConnection itself is built by an injected factory so tests can
swap in a stub without dragging the real
:mod:`aiolibdatachannel` runtime through the unit-test path. The
default factory matches :class:`SyncRtcSession`'s configuration so we
get the same ICE / SCTP plumbing we already exercise for §4.2.3 sync.

Lifecycle is the standard ``_stop: asyncio.Event`` pattern from the
project's other schedulers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import aiohttp

from ..crypto import b64url_encode, sign_ed25519
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.story_repo import AbstractStoryRepo
from . import story_public_framing as framing

log = logging.getLogger(__name__)


#: Per-author cap on concurrent public viewer sessions. The
#: 11th + viewer receives an :func:`error_frame` and the channel
#: closes immediately.
MAX_CONCURRENT_VIEWERS_PER_INSTANCE: int = 50

#: Per-story cap, applied on top of the per-instance cap. A single
#: viral story can't drown out other publications.
MAX_CONCURRENT_VIEWERS_PER_STORY: int = 10


# ─── Peer abstraction ──────────────────────────────────────────────────


@runtime_checkable
class _AnswererPeer(Protocol):
    """Protocol the signalling handler expects from a PeerConnection.

    Tests inject a stub that records calls and exposes hooks; the real
    implementation in :func:`_default_peer_factory` wraps an
    ``aiolibdatachannel.PeerConnection`` with the answerer role.
    """

    async def set_remote_offer(self, sdp: str) -> None: ...
    async def create_answer(self) -> str: ...
    async def add_ice_candidate(self, candidate: dict) -> None: ...
    async def wait_open(self) -> None: ...
    async def send(self, frame_bytes: bytes) -> None: ...
    async def close(self) -> None: ...


PeerFactory = Callable[[list[dict[str, Any]]], _AnswererPeer]


# ─── Session state ─────────────────────────────────────────────────────


@dataclass(slots=True)
class _Session:
    session_id: str
    story_id: str
    peer: _AnswererPeer
    task: asyncio.Task[None] | None = None
    pending_ice: list[dict] = field(default_factory=list)


# ─── Handler ───────────────────────────────────────────────────────────


class StorySignalingHandler:
    """Drives the author-side half of §stories_public WebRTC."""

    __slots__ = (
        "_stories",
        "_gfs_repo",
        "_http_client",
        "_signing_key",
        "_own_instance_id",
        "_peer_factory",
        "_ice_servers",
        "_media_dir",
        "_sessions",
        "_lock",
    )

    def __init__(
        self,
        story_repo: AbstractStoryRepo,
        gfs_repo: AbstractGfsConnectionRepo,
        *,
        media_dir: str,
        peer_factory: PeerFactory | None = None,
        ice_servers: list[dict[str, Any]] | None = None,
    ) -> None:
        self._stories = story_repo
        self._gfs_repo = gfs_repo
        self._http_client: aiohttp.ClientSession | None = None
        self._signing_key: bytes | None = None
        self._own_instance_id: str = ""
        self._peer_factory = peer_factory or _default_peer_factory
        self._ice_servers: list[dict[str, Any]] = list(ice_servers or [])
        self._media_dir = media_dir
        self._sessions: dict[str, _Session] = {}
        self._lock = asyncio.Lock()

    # ── Late-bound wiring ────────────────────────────────────────────────

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        if self._http_client is None:
            self._http_client = session

    def attach_identity(
        self,
        *,
        own_instance_id: str,
        signing_key: bytes,
    ) -> None:
        self._own_instance_id = own_instance_id
        self._signing_key = signing_key

    def attach_ice_servers(self, servers: list[dict[str, Any]]) -> None:
        self._ice_servers = list(servers)

    # ── Frame entry point ───────────────────────────────────────────────

    async def handle_signal(self, frame: dict) -> None:
        """Dispatch a single ``story_signal`` frame from the WS."""
        kind = str(frame.get("kind") or "")
        if kind == "offer":
            await self._on_offer(frame)
        elif kind == "ice":
            await self._on_ice(frame)
        else:
            log.debug("story_signal: unknown kind %r — dropped", kind)

    # ── Offer path ──────────────────────────────────────────────────────

    async def _on_offer(self, frame: dict) -> None:
        session_id = str(frame.get("session_id") or "")
        story_id = str(frame.get("story_id") or "")
        sdp_offer = str(frame.get("sdp") or "")
        if not (session_id and story_id and sdp_offer):
            log.debug("story_signal offer: missing fields — dropped")
            return
        if session_id in self._sessions:
            log.debug("story_signal offer: duplicate session_id — dropped")
            return

        # Reject viewer if we've hit the caps. We still need a peer to
        # send the error frame, but we open it just long enough.
        per_story = sum(1 for s in self._sessions.values() if s.story_id == story_id)
        if (
            len(self._sessions) >= MAX_CONCURRENT_VIEWERS_PER_INSTANCE
            or per_story >= MAX_CONCURRENT_VIEWERS_PER_STORY
        ):
            log.warning(
                "story_signal: rejecting offer for story=%s (cap reached)",
                story_id,
            )
            await self._reject_with_error(session_id, sdp_offer, "backpressure")
            return

        story = await self._stories.get_story(story_id)
        if story is None or story.public_gfs_id is None:
            await self._reject_with_error(session_id, sdp_offer, "expired")
            return

        peer = self._peer_factory(self._ice_servers)
        async with self._lock:
            self._sessions[session_id] = _Session(
                session_id=session_id,
                story_id=story_id,
                peer=peer,
            )
        task = asyncio.create_task(
            self._serve(session_id, sdp_offer),
            name=f"story-signal[{session_id}]",
        )
        self._sessions[session_id].task = task

    async def _on_ice(self, frame: dict) -> None:
        session_id = str(frame.get("session_id") or "")
        candidate = frame.get("candidate")
        if not session_id or not isinstance(candidate, dict):
            return
        sess = self._sessions.get(session_id)
        if sess is None:
            return
        try:
            await sess.peer.add_ice_candidate(candidate)
        except Exception as exc:  # defensive — bad candidates shouldn't kill
            log.debug("story_signal ice: %s", exc)

    # ── Streaming ──────────────────────────────────────────────────────

    async def _serve(self, session_id: str, sdp_offer: str) -> None:
        sess = self._sessions[session_id]
        peer = sess.peer
        try:
            await peer.set_remote_offer(sdp_offer)
            answer_sdp = await peer.create_answer()
            await self._post_answer(session_id, answer_sdp)
            await peer.wait_open()
            await self._stream_story(session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # defensive
            log.warning("story_signal serve %s: %s", session_id, exc)
        finally:
            try:
                await peer.close()
            except Exception:  # defensive
                pass
            async with self._lock:
                self._sessions.pop(session_id, None)

    async def _stream_story(self, session_id: str) -> None:
        sess = self._sessions[session_id]
        story = await self._stories.get_story(sess.story_id)
        if story is None:
            await sess.peer.send(framing.error_frame("expired"))
            return
        frames = await self._stories.list_frames(sess.story_id)
        manifest = []
        for f in frames:
            byte_length = self._media_size(f.media_url)
            manifest.append(
                {
                    "frame_id": f.id,
                    "sequence": f.sequence,
                    "content_type": _content_type_for(f.media_url),
                    "byte_length": byte_length,
                    "caption_text": f.caption_text,
                    "caption_emoji": f.caption_emoji,
                    "duration_ms": f.duration_ms,
                }
            )
        story_dict = {
            "id": story.id,
            "author_user_id": story.author_user_id,
            "story_date": story.story_date,
            "expires_at": story.expires_at,
        }
        await sess.peer.send(framing.story_meta(story_dict, manifest))

        for f in frames:
            await self._stream_frame(session_id, f)
        await sess.peer.send(framing.stream_end())

    async def _stream_frame(self, session_id: str, frame_row) -> None:
        sess = self._sessions[session_id]
        path = self._resolve_media_path(frame_row.media_url)
        if path is None or not path.is_file():
            log.warning(
                "story_signal: missing media for frame %s, sending error",
                frame_row.id,
            )
            await sess.peer.send(framing.error_frame("expired"))
            return
        size = path.stat().st_size
        chunk_index = 0
        sent = 0
        with open(path, "rb") as fh:
            while True:
                buf = fh.read(framing.CHUNK_SIZE)
                if not buf:
                    break
                sent += len(buf)
                is_last = sent >= size
                await sess.peer.send(
                    framing.frame_chunk(
                        frame_id=frame_row.id,
                        sequence=frame_row.sequence,
                        chunk_index=chunk_index,
                        byte_length=size,
                        is_last_chunk=is_last,
                        payload=buf,
                    )
                )
                chunk_index += 1

    def _resolve_media_path(self, media_url: str | None) -> pathlib.Path | None:
        """Resolve ``/api/media/{filename}`` to the on-disk path.

        Defends against path traversal — ``filename`` must not contain
        a slash or start with a dot, same guard
        :class:`MediaServeView` applies on the HTTP path.
        """
        if not media_url:
            return None
        prefix = "/api/media/"
        if not media_url.startswith(prefix):
            return None
        filename = media_url[len(prefix) :]
        if "/" in filename or "\\" in filename or filename.startswith("."):
            return None
        return pathlib.Path(self._media_dir) / filename

    def _media_size(self, media_url: str | None) -> int:
        path = self._resolve_media_path(media_url)
        try:
            return path.stat().st_size if path is not None else 0
        except OSError:
            return 0

    # ── HTTP back-channel to GFS ────────────────────────────────────────

    async def _post_answer(self, session_id: str, sdp: str) -> None:
        sess = self._sessions.get(session_id)
        if sess is None:
            return
        url = await self._gfs_url("/gfs/story_rtc/answer", sess.story_id)
        if url is None:
            return
        body = self._sign(
            {
                "instance_id": self._require_instance_id(),
                "session_id": session_id,
                "sdp": sdp,
            }
        )
        await self._post(url, body)

    async def _reject_with_error(
        self,
        session_id: str,
        sdp_offer: str,
        reason: str,
    ) -> None:
        """Best-effort error-frame delivery without spinning a session.

        We need a PeerConnection to actually send bytes; for cap /
        expired rejections we don't keep one around. The viewer will
        time out on its session poll, which the SPA bootstrap surfaces
        as a "the host is busy, try again" message. The reason string
        is logged so operators can spot abuse patterns.
        """
        log.info(
            "story_signal: rejecting session %s (%s) — viewer will time out",
            session_id,
            reason,
        )

    async def _gfs_url(self, path: str, story_id: str) -> str | None:
        """Resolve the inbox URL of the GFS the story is published to.

        Reads ``story.public_gfs_id`` (set during publish) so we always
        post the answer to the same GFS that pushed us the offer —
        important when the SH is paired with multiple GFSes.
        """
        if self._http_client is None:
            return None
        story = await self._stories.get_story(story_id)
        if story is None or story.public_gfs_id is None:
            return None
        conn = await self._gfs_repo.get(story.public_gfs_id)
        if conn is None or conn.status != "active":
            return None
        return f"{conn.inbox_url}{path}"

    async def _post(self, url: str, body: dict) -> None:
        if self._http_client is None:
            return
        try:
            async with self._http_client.post(
                url,
                json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status >= 300:
                    log.warning(
                        "story_signal POST %s returned HTTP %s",
                        url,
                        resp.status,
                    )
        except aiohttp.ClientError as exc:
            log.warning("story_signal POST %s failed: %s", url, exc)

    # ── Helpers ────────────────────────────────────────────────────────

    def _require_instance_id(self) -> str:
        if not self._own_instance_id:
            raise RuntimeError(
                "StorySignalingHandler used before attach_identity",
            )
        return self._own_instance_id

    def _sign(self, body: dict) -> dict:
        if self._signing_key is None:
            raise RuntimeError(
                "StorySignalingHandler used before attach_identity",
            )
        canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        signed = dict(body)
        signed["signature"] = b64url_encode(sign_ed25519(self._signing_key, canonical))
        return signed

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def stop(self) -> None:
        """Cancel every in-flight session. Called from app cleanup."""
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for sess in sessions:
            if sess.task is not None and not sess.task.done():
                sess.task.cancel()
            try:
                await sess.peer.close()
            except Exception:  # defensive
                pass


# ─── Defaults ──────────────────────────────────────────────────────────


def _content_type_for(media_url: str | None) -> str:
    """Derive a Content-Type hint from the media URL extension.

    Used for the manifest, so the JS viewer knows whether to render an
    ``<img>`` or a ``<video>`` for each chunked frame.
    """
    if not media_url:
        return "application/octet-stream"
    ext = os.path.splitext(media_url)[1].lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
    }.get(ext, "application/octet-stream")


def _default_peer_factory(_ice: list[dict[str, Any]]) -> _AnswererPeer:
    """Lazy-imports :mod:`aiolibdatachannel` so unit tests that swap
    the factory don't pay the import cost. Production code wires in
    this factory at startup; tests inject a stub.
    """
    raise NotImplementedError(
        "Production peer factory not yet wired — set "
        "``StorySignalingHandler(peer_factory=...)`` at construction "
        "or call ``attach_peer_factory`` from app startup."
    )
