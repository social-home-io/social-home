"""WebRTC DataChannel sync transport (§4.2.3, §24.12.3, §25.6.2).

Establishes a direct WebRTC DataChannel between two paired Social Home
instances for Tier 2 / Tier 3 progressive sync. The federation relay is
used for the SDP / ICE handshake only — bulk sync data flows over the
DataChannel itself, never through the relay.

``aiolibdatachannel`` is a hard runtime dependency — WebRTC is the primary
transport for sync (and for federation in general, §24.12), with the
relay HTTPS inbox only as fallback. Missing the native binding is treated
as a hard configuration error.

Security audit findings addressed here (§25.6.2):
    * **S-13** — :meth:`SyncRtcSession.create_answer` and
      :meth:`SyncRtcSession.set_answer` are distinct: the requester
      generates an answer from a remote offer, the provider processes
      the answer to complete the offer/answer exchange.
    * **S-14** — :class:`SyncRtcSession` carries an explicit
      ``requester_instance_id`` field; the answer-origin guard checks
      that field.
    * **S-16** — ``sync_mode`` is a formal constructor field with a
      default of ``"initial"``. No ``getattr(...)`` guards.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import aiolibdatachannel as rtc

from ..domain.federation import FederationEventType

log = logging.getLogger(__name__)


def _build_rtc_config(ice_servers: list[dict]) -> rtc.RTCConfiguration:
    """Flatten a Chrome-style ``ice_servers`` list. Same semantics as
    :func:`socialhome.federation.transport._build_rtc_config`.
    """
    servers: list[rtc.IceServer] = []
    for srv in ice_servers:
        url_field = srv["urls"]
        raw_urls = url_field if isinstance(url_field, list) else [url_field]
        username = srv.get("username") or None
        credential = srv.get("credential") or None
        for url in raw_urls:
            servers.append(
                rtc.IceServer(url=url, username=username, credential=credential),
            )
    return rtc.RTCConfiguration(ice_servers=servers)


# ─── Constants ────────────────────────────────────────────────────────────

#: How long :meth:`SyncRtcSession.wait_ready` blocks before giving up
#: and signalling the caller to fall back to relay chunks.
ICE_TIMEOUT_SECONDS: float = 15.0

#: DataChannel label used for sync — distinct from the GFS ``"gfs-v1"``
#: channel so a single peer can host both at once.
CHANNEL_LABEL: str = "sync-v1"

#: Maximum simultaneous signalling sessions a single node will accept
#: (S-8 cap).  Beyond this cap the manager replies with
#: ``SPACE_SYNC_DIRECT_FAILED {reason: "rate_limited"}``.
MAX_SIGNALING_SESSIONS: int = 200

#: High-water mark for a sync DataChannel's send buffer. Chunks are
#: capped at CHUNK_SIZE_BUDGET_BYTES (§exporter.py), so 1 MiB keeps
#: roughly 128 chunks buffered before we pause — plenty of headroom
#: while still bounding memory growth on a stalled peer.
SEND_HWM_BYTES: int = 1 << 20


# ─── SyncRtcSession ───────────────────────────────────────────────────────


class SyncRtcSession:
    """WebRTC DataChannel session used for direct space sync.

    Parameters
    ----------
    sync_id:
        128-bit token (`secrets.token_urlsafe(16)`) identifying the
        sync exchange.
    space_id:
        Space being synced.
    requester_instance_id:
        The instance that originated ``SPACE_SYNC_BEGIN``.  Persisted as
        a formal field per **S-14** so the answer-origin guard works.
    provider_instance_id:
        The instance that holds the canonical data and creates the offer.
    sync_mode:
        ``"initial"`` (Tier 1), ``"incremental"`` (Tier 2 — request_more
        only), or ``"full"`` (Tier 3 — full history).  Persisted as a
        formal field per **S-16**.
    role:
        ``"provider"`` (default) or ``"requester"`` — controls which
        ``create_*`` method is allowed.
    ice_servers:
        STUN / TURN configuration list passed straight to
        ``libdatachannel.IceServer``.
    """

    __slots__ = (
        "sync_id",
        "space_id",
        "requester_instance_id",
        "provider_instance_id",
        "sync_mode",
        "role",
        "_ice_servers",
        "_pc",
        "_channel",
        "_ready",
        "_remote_sdp",
        "_local_sdp",
        "_ice_candidates",
        "_loop",
        "_closed",
        "_signaling_send",
        "_local_description_applied",
    )

    def __init__(
        self,
        *,
        sync_id: str,
        space_id: str,
        requester_instance_id: str,
        provider_instance_id: str,
        sync_mode: str = "initial",
        role: str = "provider",
        ice_servers: list[dict] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        signaling_send: Callable[[FederationEventType, dict], Awaitable[None]]
        | None = None,
    ) -> None:
        if sync_mode not in ("initial", "incremental", "full"):
            raise ValueError(f"Invalid sync_mode: {sync_mode!r}")
        if role not in ("provider", "requester"):
            raise ValueError(f"Invalid role: {role!r}")

        self.sync_id = sync_id
        self.space_id = space_id
        self.requester_instance_id = requester_instance_id
        self.provider_instance_id = provider_instance_id
        self.sync_mode = sync_mode
        self.role = role
        self._ice_servers = ice_servers or []
        self._pc: Any = None  # set by _init_real_pc()
        self._channel: Any | None = None
        self._ready = asyncio.Event()
        self._remote_sdp: str | None = None
        self._local_sdp: str | None = None
        self._ice_candidates: list[str] = []
        self._loop = loop
        self._closed = False
        #: Outbound signaling callback for ``SPACE_SYNC_ICE`` envelopes.
        #: When ``None`` (older call sites or tests that don't need
        #: outbound ICE), local candidates are silently dropped — but
        #: that's exactly the bug PR-RTC-fix-3 fixed: every production
        #: SyncSessionManager wires this so the peer ever sees our
        #: candidates.
        self._signaling_send = signaling_send
        #: Set once :meth:`create_offer` or :meth:`create_answer` lands
        #: a local SDP so :meth:`_drain_ice` knows it can start
        #: forwarding candidates. ICE gathering on aiolibdatachannel
        #: starts at ``set_local_description`` (auto-negotiation
        #: disabled) — but firing ``SPACE_SYNC_ICE`` envelopes before
        #: the peer has our OFFER/ANSWER is the same race pattern that
        #: ``transport.py:`` solved by deferring ``_drain_ice`` spawn
        #: until after the signaling envelope was queued.
        self._local_description_applied = asyncio.Event()

        self._init_real_pc()

    # ─── Real aiolibdatachannel setup ─────────────────────────────────────

    def _init_real_pc(self) -> None:
        """Configure a real ``aiolibdatachannel.PeerConnection``."""
        self._pc = rtc.PeerConnection(_build_rtc_config(self._ice_servers))

    async def _open_channel_async(self) -> None:
        """Lazy channel setup — started at offer/answer time so we are
        inside an event loop with a known :class:`asyncio.get_running_loop`.

        Uses ``pc.spawn_task`` so tasks are auto-cancelled when the
        PeerConnection closes.
        """
        if self._channel is not None:
            return
        if self.role == "provider":
            self._channel = await self._pc.create_data_channel(CHANNEL_LABEL)
            self._channel.set_buffered_amount_low_threshold(
                SEND_HWM_BYTES // 2,
            )
            self._pc.spawn_task(self._watch_channel(self._channel))
        else:
            self._pc.spawn_task(self._watch_incoming())
        # Drain local ICE candidates outbound via ``SPACE_SYNC_ICE`` so
        # the peer can complete connectivity checks. Pre-PR-RTC-fix-3
        # this drain didn't exist — local candidates were generated
        # into ``pc.ice_candidates()``'s queue but nothing consumed
        # them, so the peer only ever saw the SDP offer's host
        # candidate (loopback only) and ICE failed on anything that
        # required STUN-mapped candidates. Tier 2/3 sync would then
        # time out on ``wait_ready`` and fall back to relay every
        # time. Deferred-spawn pattern mirrors ``transport.py``'s
        # ``_drain_ice`` — local candidates must not race past the
        # OFFER/ANSWER envelope, so we wait on
        # ``_local_description_applied`` inside the loop.
        if self._signaling_send is not None:
            self._pc.spawn_task(self._drain_ice())

    async def _watch_incoming(self) -> None:
        try:
            async for ch in self._pc.incoming_data_channels():
                if ch.label != CHANNEL_LABEL:
                    continue
                self._channel = ch
                ch.set_buffered_amount_low_threshold(SEND_HWM_BYTES // 2)
                self._pc.spawn_task(self._watch_channel(ch))
                return
        except asyncio.CancelledError:
            raise
        except (rtc.RTCError, rtc.ConnectionClosedError) as exc:
            log.debug(
                "SyncRtcSession[%s]: incoming-channel wait ended: %s",
                self.sync_id,
                exc,
            )

    async def _drain_ice(self) -> None:
        """Forward every local ICE candidate to the peer over signalling.

        Gates the first send on :attr:`_local_description_applied` so
        we don't ship candidates before the OFFER/ANSWER envelope is
        on its way to the peer — the same race-protection
        ``transport.py:_drain_ice`` (PR #371) added for the federation
        channel. Without this drain the peer never receives our
        candidates and ICE connectivity-checks fail; the DataChannel
        never opens; sync falls back to relay.
        """
        pc = self._pc
        if pc is None or self._signaling_send is None:
            return
        await self._local_description_applied.wait()
        try:
            async for cand in pc.ice_candidates():
                if pc is not self._pc:  # PC swapped under us (close)
                    return
                payload = {
                    "sync_id": self.sync_id,
                    "candidate": cand.candidate,
                    "sdp_mid": cand.mid,
                }
                try:
                    await self._signaling_send(
                        FederationEventType.SPACE_SYNC_ICE,
                        payload,
                    )
                except Exception:
                    log.warning(
                        "SyncRtcSession[%s]: SPACE_SYNC_ICE signal failed",
                        self.sync_id,
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            raise
        except (rtc.RTCError, rtc.ConnectionClosedError) as exc:
            log.debug(
                "SyncRtcSession[%s]: ICE drain ended: %s",
                self.sync_id,
                exc,
            )

    async def _watch_channel(self, channel) -> None:
        try:
            await channel.wait_open()
        except (rtc.RTCError, rtc.ConnectionClosedError) as exc:
            log.warning(
                "SyncRtcSession[%s]: channel failed to open: %s",
                self.sync_id,
                exc,
            )
            return
        log.info(
            "SyncRtcSession[%s]: DataChannel open (space=%s, mode=%s)",
            self.sync_id,
            self.space_id,
            self.sync_mode,
        )
        self._ready.set()
        try:
            await channel.wait_closed()
        except rtc.RTCError, rtc.ConnectionClosedError:
            pass
        log.info("SyncRtcSession[%s]: DataChannel closed", self.sync_id)
        self._closed = True

    # ─── Provider role ────────────────────────────────────────────────────

    async def create_offer(self) -> str:
        """Generate an SDP offer (provider role)."""
        if self.role != "provider":
            raise RuntimeError("create_offer is only valid for provider sessions")

        await self._open_channel_async()
        local = await self._pc.set_local_description("offer")
        self._local_sdp = local.sdp
        # Unblock ``_drain_ice`` — the caller will ship the SDP offer
        # via ``SPACE_SYNC_OFFER`` right after this returns, so
        # candidates that fire next are race-safe against the peer's
        # ``apply_offer``.
        self._local_description_applied.set()
        return local.sdp

    async def set_answer(self, sdp_answer: str) -> None:
        """Apply the SDP answer (provider role) — completes negotiation.

        Distinct from :meth:`create_answer` (which is for the requester
        side).  Per **S-13** never use ``set_answer`` to handle an
        incoming offer.
        """
        if self.role != "provider":
            raise RuntimeError("set_answer is only valid for provider sessions")
        if not sdp_answer:
            raise ValueError("Empty SDP answer")

        self._remote_sdp = sdp_answer
        await self._pc.set_remote_description(sdp_answer, "answer")

    # ─── Requester role ───────────────────────────────────────────────────

    async def create_answer(self, sdp_offer: str) -> str:
        """Generate an SDP answer (requester role)."""
        if self.role != "requester":
            raise RuntimeError("create_answer is only valid for requester sessions")
        if not sdp_offer:
            raise ValueError("Empty SDP offer")

        await self._open_channel_async()
        self._remote_sdp = sdp_offer
        await self._pc.set_remote_description(sdp_offer, "offer")
        local = await self._pc.set_local_description("answer")
        self._local_sdp = local.sdp
        # Unblock ``_drain_ice`` (see ``create_offer`` for the
        # rationale).
        self._local_description_applied.set()
        return local.sdp

    # ─── Shared ───────────────────────────────────────────────────────────

    async def add_ice_candidate(self, candidate: str, sdp_mid: str = "0") -> None:
        """Add a remote ICE candidate received via ``SPACE_SYNC_ICE``.

        The SyncSessionManager validates the candidate (size + format)
        before calling this method per **S-7**.
        """
        if not candidate:
            raise ValueError("Empty ICE candidate")
        self._ice_candidates.append(candidate)
        await self._pc.add_remote_candidate(candidate, sdp_mid)

    async def wait_ready(self, timeout: float = ICE_TIMEOUT_SECONDS) -> bool:
        """Block until the DataChannel is open, or *timeout* seconds elapse.

        Returns ``True`` on open, ``False`` on timeout.
        """
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def send_chunk(self, chunk_payload: bytes | str) -> None:
        """Send a ``SPACE_SYNC_CHUNK`` frame over the DataChannel.

        Raises :class:`ConnectionError` if the channel is not open or
        if the send buffer is over the high-water mark — the sync
        manager handles the latter by falling back to relay chunks.
        """
        if self._channel is None or self._closed:
            raise ConnectionError("DataChannel not open")
        if self._channel.buffered_amount >= SEND_HWM_BYTES:
            raise ConnectionError(
                f"DataChannel backpressured "
                f"(buffered={self._channel.buffered_amount}, "
                f"hwm={SEND_HWM_BYTES})",
            )
        await self._channel.send(chunk_payload)

    @property
    def is_ready(self) -> bool:
        """Whether the DataChannel has signalled ``onOpen``."""
        return self._ready.is_set()

    @property
    def is_closed(self) -> bool:
        """Whether the channel has been closed (locally or remotely)."""
        return self._closed

    def close(self) -> None:
        """Close the underlying connection and mark the session closed.

        ``pc.close()`` tears down the PeerConnection; aiolibdatachannel
        auto-cancels any tasks registered via ``pc.spawn_task``.
        """
        self._closed = True
        self._ready.clear()
        if self._pc is not None:
            try:
                self._pc.close()
            except rtc.RTCError:
                pass
        self._pc = None
        self._channel = None


# ─── Stateful helpers used by the manager ────────────────────────────────


@dataclass(slots=True)
class SyncSessionRecord:
    """Lightweight record for the in-memory session registry."""

    sync_id: str
    space_id: str
    requester_instance_id: str
    provider_instance_id: str
    sync_mode: str
    rtc: SyncRtcSession | None = None
    created_at: float = field(default=0.0)
    #: Spec §24.10.7 — URL of the GFS cluster node assigned to relay
    #: ICE candidates for this session. Set by the provider on
    #: ``SPACE_SYNC_OFFER`` build, read on ``DIRECT_READY``/``FAILED``
    #: to release the GFS counter. ``None`` for single-node GFS or
    #: HFS-only deployments.
    signaling_node: str | None = None
