"""Federation delivery transport (§24.12, §4.2.3) — aiolibdatachannel edition.

The :class:`FederationTransport` facade is the single delivery seam for
outbound federation events. It keeps one :class:`_RtcPeer` per paired
peer and switches between two transports at send time:

* **WebRTC DataChannel** — primary transport. Once the DTLS + SRTP
  negotiation completes the channel stays open for the lifetime of the
  peering; routine envelopes go over it with zero HTTP overhead.
* **HTTPS inbox** — fallback transport and bootstrap path. Used (a)
  before the DataChannel is established (the signed SDP offer/answer
  and ICE candidates ride on top of it), (b) whenever the channel is
  closed / failing, and (c) to reach peers behind a strictly-blocked
  UDP path.

The channel payload is identical to the HTTPS inbox payload: the caller
still builds the AES-256-GCM-encrypted + Ed25519-signed
:class:`FederationEnvelope` the same way. Only delivery differs.

Security invariants:

* **S-14 (answer-origin)** — an inbound
  ``FEDERATION_RTC_ANSWER`` must come from the peer we sent the offer
  to. :class:`_RtcPeer` tracks the expected responder and rejects
  mismatched answers with a warning log.
* **Sender signature** — RTC frames are plain UTF-8 JSON of the same
  envelope dict the HTTPS inbox transport would have POSTed. The Ed25519
  signature inside the envelope proves origin; DTLS protects the
  DataChannel against an on-path MITM but the envelope signature is
  what the receiving :class:`FederationService` actually checks.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import aiolibdatachannel as rtc
import orjson
from aiohttp import ClientTimeout

from ..domain.events import PeerTransportChanged
from ..domain.federation import DeliveryResult, FederationEventType, RemoteInstance

if TYPE_CHECKING:
    from ..infrastructure.event_bus import EventBus

log = logging.getLogger(__name__)


# ─── Config ─────────────────────────────────────────────────────────────────

#: DataChannel label for federation-wide event traffic. Distinct from
#: ``sync-v1`` (§4.2.3) so sync + routine federation can coexist.
CHANNEL_LABEL: str = "fed-v1"

#: Maximum time we will wait for the DataChannel to finish negotiating
#: before giving up and falling back to HTTPS inbox.
RTC_READY_TIMEOUT_S: float = 10.0

#: Keep-alive interval once the channel is open. Matches the TS
#: client's 30 s cadence (spec §24.12.5).
PING_INTERVAL_S: float = 30.0

#: High-water mark for a DataChannel's send buffer. When
#: ``dc.buffered_amount`` exceeds this, we drop the frame and let the
#: caller fall back to HTTPS inbox instead of unbounded SCTP queuing.
#: 1 MiB is well above a single envelope (~10 KB) but far under the
#: default libdatachannel message size ceiling.
SEND_HWM_BYTES: int = 1 << 20


# ─── HTTPS inbox transport ─────────────────────────────────────────────────────


class HttpsInboxTransport:
    """HTTPS POST transport — always available, used as fallback.

    Thin wrapper around an aiohttp client session. Keeping it a class
    (rather than a bare function) lets tests swap it out without
    patching module-level state.
    """

    __slots__ = ("_client_factory", "_client", "_timeout_s")

    def __init__(
        self,
        client_factory: Callable[[], Awaitable[Any]],
        *,
        timeout_s: float = 10.0,
    ) -> None:
        self._client_factory = client_factory
        self._client: Any | None = None
        self._timeout_s = timeout_s

    async def _client_once(self) -> Any:
        if self._client is None:
            self._client = await self._client_factory()
        return self._client

    async def send(
        self,
        *,
        instance: RemoteInstance,
        envelope_dict: dict,
    ) -> tuple[bool, int | None]:
        """POST the envelope to the remote inbox URL.

        Returns ``(ok, status_code)``. ``ok`` is true iff the peer
        returned 2xx. Any network-level error returns
        ``(False, None)`` so the caller can record a failure and
        enqueue for retry.
        """
        try:
            client = await self._client_once()
            async with client.post(
                instance.remote_inbox_url,
                json=envelope_dict,
                timeout=ClientTimeout(total=self._timeout_s),
            ) as resp:
                status = resp.status
                return 200 <= status < 300, status
        except Exception as exc:
            log.warning(
                "HTTPS-inbox send to %s failed: %s",
                instance.id,
                exc,
            )
            return False, None


# ─── RTC peer ──────────────────────────────────────────────────────────────

# The frame format on the DataChannel is the same envelope dict the
# HTTPS inbox transport would have POSTed — serialised as UTF-8 JSON with
# orjson for consistency with the HTTPS-inbox path.
_InboundCallback = Callable[[dict], Awaitable[None]]


def _build_rtc_config(ice_servers: list[dict]) -> rtc.RTCConfiguration:
    """Flatten a Chrome-style ``ice_servers`` list into an
    :class:`aiolibdatachannel.RTCConfiguration`.

    Each entry may carry a single ``urls`` string or a list of them
    plus optional TURN ``username`` / ``credential``. We map each URL
    to an :class:`~aiolibdatachannel.IceServer` so credentials ride as
    first-class fields rather than being spliced into URL userinfo.
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


#: How long an ICE candidate may sit in the buffer before being dropped.
#: A trickled candidate can outrun the matching OFFER/ANSWER on independent
#: HTTPS inbox round-trips (separate sockets, separate retry timers), and a
#: stale outbox at the sender can stretch that gap to tens of seconds. The
#: timeout has to cover that — dropping the candidate is unrecoverable,
#: while parking it costs a few KiB of memory until the handshake either
#: completes or the peer is torn down. Tuned for the HA-add-on relay path
#: where SDP can cross continents and queue behind retries.
ICE_BUFFER_TIMEOUT_S: float = 30.0


class _RtcPeer:
    """One DataChannel session for one paired peer."""

    __slots__ = (
        "instance_id",
        "_ice_servers",
        "_signaling",
        "_inbound",
        "_pc",
        "_channel",
        "_open",
        "_closed",
        "_loop",
        "_expected_answer_from",
        "_send_hwm",
        "_remote_description_applied",
        "_bus",
        "_published_open",
        "_polite",
        "_making_offer",
        "_on_failed",
        "_teardown_task",
    )

    def __init__(
        self,
        *,
        instance_id: str,
        ice_servers: list[dict] | None,
        signaling: Callable[[FederationEventType, dict], Awaitable[None]],
        inbound: _InboundCallback,
        send_hwm: int = SEND_HWM_BYTES,
        bus: "EventBus | None" = None,
        polite: bool = False,
        on_failed: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.instance_id = instance_id
        self._ice_servers = ice_servers or []
        self._signaling = signaling
        self._inbound = inbound
        self._pc: Any | None = None
        self._channel: Any | None = None
        self._open = asyncio.Event()
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        # Notified when the PC transitions to ``RTCState.FAILED`` so the
        # parent transport can evict this peer from its registry and
        # let the next ``send()`` build a fresh handshake instead of
        # silently falling back to HTTPS forever.
        self._on_failed = on_failed
        # Strong ref so a teardown publish from ``close()`` doesn't get
        # GC'd before it runs (CPython's loop holds tasks via a weakref
        # set in some implementations).
        self._teardown_task: asyncio.Task | None = None
        # S-14: on the offerer side we lock the answer origin to the
        # peer we invited. Mismatches are rejected with a warning.
        self._expected_answer_from: str | None = None
        self._send_hwm = send_hwm
        # WebRTC requires every ``addRemoteCandidate`` call to land
        # *after* ``setRemoteDescription`` — applying earlier throws
        # ``rtcAddRemoteCandidate: runtime failure``. Trickle ICE
        # routinely violates this on the wire because the offerer
        # flushes candidates the moment the offer is sent, and the
        # receiver may see ``FEDERATION_RTC_ICE`` envelopes before (or
        # mid-construction of) the matching ``FEDERATION_RTC_OFFER``.
        # The event is set by :meth:`accept_offer` and
        # :meth:`apply_answer` once the remote description is in;
        # :meth:`add_ice_candidate` waits on it.
        self._remote_description_applied = asyncio.Event()
        self._bus = bus
        self._published_open = False
        # Perfect-negotiation role: polite peer defers to an incoming
        # OFFER by rolling back its own pending offer; impolite peer
        # ignores an incoming OFFER when one is already in flight.
        # Determined lexicographically: own_instance_id > peer_id.
        self._polite = polite
        # Set True between ``start_offer`` initiation and either the
        # successful ``apply_answer`` call or a polite rollback.
        self._making_offer: bool = False

    # ─── Transport change publication ─────────────────────────────────────

    async def _publish_open_if_needed(self) -> None:
        """Publish ``PeerTransportChanged(transport='rtc')`` exactly once
        per channel-open edge. Called from ``_drain_channel`` when the
        DataChannel transitions to OPEN.
        """
        if self._published_open or self._bus is None:
            return
        self._published_open = True
        await self._bus.publish(
            PeerTransportChanged(
                instance_id=self.instance_id,
                transport="rtc",
            ),
        )

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def start_offer(self) -> None:
        """Initiate the SDP offer/answer handshake (offerer role)."""
        self._making_offer = True
        self._expected_answer_from = self.instance_id
        self._loop = asyncio.get_running_loop()
        self._pc = rtc.PeerConnection(_build_rtc_config(self._ice_servers))
        self._channel = await self._pc.create_data_channel(CHANNEL_LABEL)
        # Ask aiolibdatachannel to notify us once the buffered amount
        # drops below half the HWM — lets future refactors await
        # backpressure instead of polling. For now we just read
        # ``buffered_amount`` directly in ``send()``.
        self._channel.set_buffered_amount_low_threshold(self._send_hwm // 2)
        # Tasks bound to the pc: auto-cancelled on pc.close().
        self._pc.spawn_task(self._drain_channel(self._channel))
        self._pc.spawn_task(self._drain_events())

        local = await self._pc.set_local_description("offer")
        await self._signaling(
            FederationEventType.FEDERATION_RTC_OFFER,
            {"sdp": local.sdp, "sdp_type": local.type},
        )
        # Only now: start forwarding trickled ICE candidates. ``_drain_ice``
        # and the OFFER/ANSWER signaling are independent HTTPS posts to the
        # peer's inbox; if the drain task starts before the OFFER is queued,
        # a candidate POST can outrun the OFFER POST on the wire and the
        # receiver drops the candidate at the §24.12.5 buffer timeout.
        # Gathering keeps producing candidates inside libdatachannel's queue
        # until we start consuming, so deferral is lossless.
        self._pc.spawn_task(self._drain_ice())

    async def accept_offer(self, *, sdp: str, from_instance: str) -> None:
        """Receive an SDP offer (answerer role) and reply with an answer.

        Implements perfect-negotiation glare resolution: if we are
        already making an offer (or the PC is not in stable state),
        the impolite peer ignores this incoming OFFER and the polite
        peer rolls back its own offer before accepting.
        """
        # The signaling state comes back as ``aiolibdatachannel.SignalingState``
        # which is an ``IntEnum`` — comparing it against the string
        # ``"stable"`` always returns ``True`` (IntEnum vs str), so before
        # this fix every legitimate handshake was treated as a glare
        # collision. Compare against the enum value directly. See also
        # :meth:`apply_answer` below.
        collision = self._making_offer or (
            self._pc is not None
            and self._pc.signaling_state != rtc.SignalingState.STABLE
        )
        if collision and not self._polite:
            log.info(
                "RTC glare: impolite side ignoring incoming OFFER from %s "
                "(making_offer=%s, signaling_state=%s)",
                from_instance,
                self._making_offer,
                self._pc.signaling_state if self._pc else None,
            )
            return
        if collision and self._polite:
            log.info(
                "RTC glare: polite side rolling back our OFFER to accept %s's",
                from_instance,
            )
            await self._close_pc()
            self._making_offer = False

        self._expected_answer_from = None  # answerer, no outstanding offer
        self._loop = asyncio.get_running_loop()
        self._pc = rtc.PeerConnection(_build_rtc_config(self._ice_servers))

        self._pc.spawn_task(self._drain_incoming_channel())
        self._pc.spawn_task(self._drain_events())

        await self._pc.set_remote_description(sdp, "offer")
        # Release any ICE candidates that arrived before the offer (or
        # during the construction of the PC). They'll have been parked
        # in :meth:`add_ice_candidate` waiting on this event.
        self._remote_description_applied.set()
        local = await self._pc.set_local_description("answer")
        await self._signaling(
            FederationEventType.FEDERATION_RTC_ANSWER,
            {"sdp": local.sdp, "sdp_type": local.type},
        )
        # Only now: start forwarding trickled ICE candidates — see the
        # matching comment in :meth:`start_offer` for the wire-ordering
        # rationale. The original code spawned this task before
        # ``set_local_description("answer")``, which let a candidate POST
        # race the ANSWER POST and arrive at the offerer first; the
        # offerer then dropped the candidate at the buffer timeout
        # because its remote description hadn't been applied yet.
        self._pc.spawn_task(self._drain_ice())

    async def apply_answer(self, *, sdp: str, from_instance: str) -> bool:
        """Apply the peer's SDP answer to our pending offer.

        Returns ``True`` when accepted. Rejects (returns ``False``) if
        ``from_instance`` doesn't match the peer we sent the offer to —
        S-14 answer-origin guard. Also ignores late answers that arrive
        after a polite-side rollback (signaling_state will no longer be
        ``have-local-offer``).
        """
        if (
            self._expected_answer_from is not None
            and from_instance != self._expected_answer_from
        ):
            log.warning(
                "RTC answer for %s rejected — came from %s",
                self._expected_answer_from,
                from_instance,
            )
            return False
        # Perfect-negotiation guard: if we rolled back our offer (polite
        # side) the PC is either None or no longer in have-local-offer
        # state — silently drop this stale ANSWER.  ``signaling_state``
        # is ``IntEnum``; comparing against the string ``"have-local-offer"``
        # was always True (IntEnum vs str), so prior to this fix every
        # legitimate answer was rejected and the offerer's handshake
        # never completed → silent HTTPS fallback.
        if (
            self._pc is None
            or self._pc.signaling_state != rtc.SignalingState.HAVE_LOCAL_OFFER
        ):
            log.info(
                "RTC answer from %s ignored — pc is %s",
                from_instance,
                self._pc.signaling_state if self._pc else "absent",
            )
            return False
        self._expected_answer_from = None
        await self._pc.set_remote_description(sdp, "answer")
        # Offerer side: flush any ICE candidates the answerer
        # trickled before we applied their SDP answer. Same
        # rationale as :meth:`accept_offer`.
        self._remote_description_applied.set()
        self._making_offer = False
        return True

    async def add_ice_candidate(self, *, candidate: str, sdp_mid: str) -> None:
        """Apply a trickled ICE candidate, buffering until the remote
        description is in.

        WebRTC requires every ``addRemoteCandidate`` call to land after
        ``setRemoteDescription``; aiolibdatachannel surfaces a
        violation as ``rtcAddRemoteCandidate: runtime failure (-2)``
        and the candidate is silently lost — which then strands ICE
        with no remote candidates, the connectivity timer expires, and
        the PeerConnection transitions to ``failed``.

        We sidestep the race by gating every candidate on
        :attr:`_remote_description_applied`. The event is set by
        :meth:`accept_offer` (answerer) and :meth:`apply_answer`
        (offerer); until then the candidate parks here. If the
        handshake never lands within :data:`ICE_BUFFER_TIMEOUT_S` we
        drop the candidate rather than pinning memory forever.
        """
        if not candidate or self._closed:
            return
        if not self._remote_description_applied.is_set():
            try:
                await asyncio.wait_for(
                    self._remote_description_applied.wait(),
                    timeout=ICE_BUFFER_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "fed RTC: ICE candidate for %s dropped — remote "
                    "description not applied within %.0fs",
                    self.instance_id,
                    ICE_BUFFER_TIMEOUT_S,
                )
                return
        if self._pc is None or self._closed:
            # Peer closed between event set and resume — race, drop.
            return
        try:
            await self._pc.add_remote_candidate(candidate, sdp_mid)
        except rtc.RTCError as exc:
            # Native layer can still reject a malformed or late
            # candidate — log at debug rather than letting the
            # exception bubble through the event dispatcher.
            log.debug(
                "fed RTC: add_remote_candidate failed for %s: %s",
                self.instance_id,
                exc,
            )

    # ─── Internal drain loops ─────────────────────────────────────────────

    async def _drain_events(self) -> None:
        """Log every PC state transition and trigger auto-recovery on
        ``RTCState.FAILED``.

        The library exposes a unified async iterator (`pc.events()`)
        that fires for every connection / ICE / signaling / gathering
        transition. Before this loop, the only visible signal at the
        federation layer was ``channel open`` / ``channel closed`` —
        an operator looking at a stuck handshake had no way to tell
        whether ICE never gathered, DTLS failed, the answer was
        rejected, or the peer just dropped. Now every transition is
        a log line tagged with the instance.

        When the connection enters ``FAILED`` we evict ourselves from
        the parent transport via ``_on_failed`` so the next ``send()``
        builds a fresh handshake instead of returning False forever.
        """
        pc = self._pc
        if pc is None:
            return
        try:
            async for ev in pc.events():
                if isinstance(ev, rtc.StateChangeEvent):
                    log.info(
                        "fed RTC peer %s: connection_state=%s",
                        self.instance_id,
                        ev.state.name,
                    )
                    if ev.state in (rtc.RTCState.FAILED, rtc.RTCState.CLOSED):
                        # Stale event for a PC we've already rolled
                        # back away from — leave the rebuilt
                        # connection alone. Without this guard the
                        # polite-rollback path can flip
                        # ``self._closed = True`` on the **reused**
                        # ``_RtcPeer``, so the freshly-handshaked
                        # DataChannel reports ``is_ready=False``
                        # forever and federation silently falls
                        # back to HTTPS for the life of the session.
                        if self._pc is not pc:
                            continue
                        self._closed = True
                        self._open.clear()
                        if (
                            ev.state == rtc.RTCState.FAILED
                            and self._on_failed is not None
                        ):
                            # Schedule the eviction as a separate
                            # task so we don't await it inline —
                            # the callback calls ``peer.close()``
                            # which closes ``self._pc`` which
                            # cancels the very ``_drain_events``
                            # task we're in the middle of.
                            # Decoupling avoids the self-cancel
                            # (and any cleanup logic future
                            # contributors might add below this
                            # branch).
                            assert self._loop is not None
                            evict_cb = self._on_failed
                            instance_id = self.instance_id

                            async def _evict_async() -> None:
                                try:
                                    await evict_cb(instance_id)
                                except Exception as exc:  # noqa: BLE001
                                    log.warning(
                                        "fed RTC on_failed handler raised for %s: %s",
                                        instance_id,
                                        exc,
                                    )

                            self._loop.create_task(
                                _evict_async(),
                                name=f"fed-rtc-evict[{instance_id}]",
                            )
                elif isinstance(ev, rtc.IceStateChangeEvent):
                    log.info(
                        "fed RTC peer %s: ice_state=%s",
                        self.instance_id,
                        ev.state.name,
                    )
                elif isinstance(ev, rtc.SignalingStateChangeEvent):
                    log.debug(
                        "fed RTC peer %s: signaling_state=%s",
                        self.instance_id,
                        ev.state.name,
                    )
                elif isinstance(ev, rtc.GatheringStateChangeEvent):
                    log.debug(
                        "fed RTC peer %s: gathering_state=%s",
                        self.instance_id,
                        ev.state.name,
                    )
                # LocalDescription / LocalCandidate / DataChannel events
                # are consumed by their specialised iterators
                # (``ice_candidates``, ``incoming_data_channels``,
                # ``set_local_description``); skip them here so we don't
                # double-log.
        except asyncio.CancelledError:
            raise
        except (rtc.RTCError, rtc.ConnectionClosedError) as exc:
            log.debug("fed RTC events drain to %s ended: %s", self.instance_id, exc)

    async def _drain_ice(self) -> None:
        """Pump local ICE candidates out to the peer over signalling.

        **Race guard**: ``pc`` is captured at task entry. The polite-
        rollback path in :meth:`accept_offer` calls
        :meth:`_close_pc` which both closes the old PC AND swaps
        ``self._pc`` to a freshly-built one. Without the
        ``self._pc is pc`` check below, the OLD-PC's ``_drain_ice``
        (still alive in the brief window between ``pc.close()`` and
        task cancellation) can read one more candidate from the
        dying PC and ship it via ``_signaling`` to the peer. That
        candidate refers to a socket about to close — worse, the
        peer might successfully pair-check against it, briefly
        flipping ICE state to ``CONNECTED`` only to lose it again on
        DTLS. From an operator log that's exactly the
        "ICE state to connected → DTLS handshake failed" pattern.
        """
        pc = self._pc
        assert pc is not None  # spawned from start_offer/accept_offer
        try:
            async for cand in pc.ice_candidates():
                if self._pc is not pc:
                    return
                await self._signaling(
                    FederationEventType.FEDERATION_RTC_ICE,
                    {"candidate": cand.candidate, "sdp_mid": cand.mid},
                )
        except asyncio.CancelledError:
            raise
        except (rtc.RTCError, rtc.ConnectionClosedError) as exc:
            log.debug("fed RTC ICE drain to %s ended: %s", self.instance_id, exc)

    async def _drain_incoming_channel(self) -> None:
        """Answerer path: wait for the provider's DataChannel to arrive."""
        pc = self._pc
        assert pc is not None  # spawned from accept_offer
        try:
            async for ch in pc.incoming_data_channels():
                if ch.label != CHANNEL_LABEL:
                    continue
                self._channel = ch
                ch.set_buffered_amount_low_threshold(self._send_hwm // 2)
                pc.spawn_task(self._drain_channel(ch))
                return
        except asyncio.CancelledError:
            raise
        except (rtc.RTCError, rtc.ConnectionClosedError) as exc:
            log.debug("fed RTC incoming-channel wait ended: %s", exc)

    async def _drain_channel(self, channel) -> None:
        """Consume inbound frames on a DataChannel and mark open/closed."""
        try:
            await channel.wait_open()
        except (rtc.RTCError, rtc.ConnectionClosedError) as exc:
            log.warning(
                "fed RTC channel never opened to %s: %s",
                self.instance_id,
                exc,
            )
            return
        log.info("fed RTC channel open to %s", self.instance_id)
        self._open.set()
        await self._publish_open_if_needed()
        try:
            async for msg in channel:
                try:
                    data = orjson.loads(
                        msg if isinstance(msg, (bytes, str)) else bytes(msg)
                    )
                except Exception as exc:  # noqa: BLE001 — orjson raises orjson.JSONDecodeError + anything
                    log.warning(
                        "fed RTC malformed frame from %s: %s",
                        self.instance_id,
                        exc,
                    )
                    continue
                await self._inbound(data)
        except asyncio.CancelledError:
            raise
        except rtc.ConnectionClosedError:
            pass
        except rtc.RTCError as exc:
            log.debug("fed RTC recv loop to %s ended: %s", self.instance_id, exc)
        log.info("fed RTC channel closed to %s", self.instance_id)
        self._open.clear()
        self._closed = True

    # ─── Sending ──────────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        """Whether the DataChannel is currently open."""
        return self._open.is_set() and not self._closed

    async def send(self, envelope_dict: dict) -> bool:
        """Push a JSON frame over the DataChannel.

        Returns ``True`` on success, ``False`` if the channel isn't
        currently open or the send buffer is over the HWM (caller
        should fall back to HTTPS inbox). Dropping under backpressure is
        preferable to unbounded SCTP queueing.
        """
        if not self.is_ready or self._channel is None:
            return False
        buffered = self._channel.buffered_amount
        if buffered >= self._send_hwm:
            log.warning(
                "fed RTC peer %s: buffered %d ≥ HWM %d — dropping frame",
                self.instance_id,
                buffered,
                self._send_hwm,
            )
            return False
        try:
            await self._channel.send(orjson.dumps(envelope_dict))
            return True
        except (rtc.RTCError, rtc.ConnectionClosedError) as exc:
            log.warning("fed RTC send to %s failed: %s", self.instance_id, exc)
            return False

    async def _close_pc(self) -> None:
        """Close and discard the current PeerConnection, resetting negotiation state.

        Idempotent: safe to call when ``_pc`` is already ``None``.
        Used by the polite-side rollback path in perfect negotiation to
        tear down our pending offer so we can accept the peer's instead.
        """
        if self._pc is not None:
            try:
                self._pc.close()
            except rtc.RTCError:
                pass
            self._pc = None
        self._channel = None
        # Wake any coroutines parked in ``add_ice_candidate`` on the OLD
        # event before swapping in a fresh one. Without this they sit on
        # the old (never-set) event until the 30-second buffer timeout
        # fires — re-creating exactly the dropped-candidate symptom
        # PR #371 fixed. Once they wake, they either find ``_pc is None``
        # and return, or hit the new PC's ``add_remote_candidate`` and
        # get harmlessly rejected by the existing ``RTCError`` guard.
        self._remote_description_applied.set()
        self._remote_description_applied = asyncio.Event()

    def close(self) -> None:
        """Close the underlying connection and mark the peer closed.

        ``pc.close()`` tears down the PeerConnection; aiolibdatachannel
        auto-cancels any tasks registered via ``pc.spawn_task`` so we
        don't need to track them ourselves.

        If the DataChannel had previously been open, schedules a
        ``PeerTransportChanged(transport='https')`` publication via the
        running event loop so listeners know the peer has fallen back to
        HTTPS inbox. No event is emitted if the channel never opened —
        the transport never flipped away from HTTPS in the first place.
        """
        self._closed = True
        self._open.clear()
        # Wake any ICE candidates parked in
        # :meth:`add_ice_candidate` so they unblock immediately and
        # see ``_closed`` instead of waiting out the 10 s timeout.
        self._remote_description_applied.set()
        if self._pc is not None:
            try:
                self._pc.close()
            except rtc.RTCError:
                pass
        self._pc = None
        self._channel = None
        if self._published_open and self._bus is not None and self._loop is not None:
            self._published_open = False
            # Stash the task on the instance so asyncio doesn't garbage-
            # collect it before the publish coro runs. ``create_task``
            # only holds a weak ref via the loop's task set, so a
            # never-awaited handle can be reclaimed mid-flight and the
            # publish silently drops.
            self._teardown_task = self._loop.create_task(
                self._bus.publish(
                    PeerTransportChanged(
                        instance_id=self.instance_id,
                        transport="https",
                    ),
                ),
                name=f"fed-rtc-teardown[{self.instance_id}]",
            )


# ─── Facade ────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class _TransportSendResult:
    """What :meth:`FederationTransport.send` returns to the caller."""

    ok: bool
    via: str  # "rtc" | "https"
    status_code: int | None = None
    error: str | None = None


class FederationTransport:
    """Route outbound federation envelopes over RTC when possible.

    Wiring: construct with the instance's own id, a HTTPS inbox transport,
    and a callback used to dispatch the three ``FEDERATION_RTC_*``
    signalling events through :class:`FederationService.send_event`
    (which is the same signed HTTPS-inbox path used for everything else).
    """

    __slots__ = (
        "_own_instance_id",
        "_https_inbox",
        "_signaling_send",
        "_ice_servers",
        "_peers",
        "_lock",
        "_inbound_handler",
        "_bus",
    )

    def __init__(
        self,
        *,
        own_instance_id: str,
        https_inbox: HttpsInboxTransport,
        signaling_send: Callable[
            [str, FederationEventType, dict], Awaitable[DeliveryResult]
        ],
        ice_servers: list[dict] | None = None,
        inbound_handler: Callable[[str, bytes], Awaitable[dict]] | None = None,
        bus: "EventBus | None" = None,
    ) -> None:
        self._own_instance_id = own_instance_id
        self._https_inbox = https_inbox
        self._signaling_send = signaling_send
        self._ice_servers = ice_servers or []
        self._peers: dict[str, _RtcPeer] = {}
        self._lock = asyncio.Lock()
        # Callback for inbound DataChannel frames → §24.11 pipeline.
        # Signature: ``async (instance_id, raw_body) -> dict``.
        # Attached by FederationService after construction.
        self._inbound_handler = inbound_handler
        self._bus = bus

    def set_ice_servers(self, servers: list[dict]) -> None:
        """Update the ICE-server list used for *future* peer handshakes.

        Existing peers keep their original config — renegotiating live
        DataChannels is out of scope. New ``_ensure_handshake`` calls
        pick up the updated list.
        """
        self._ice_servers = list(servers or [])

    # ─── Outbound ─────────────────────────────────────────────────────────

    async def send(
        self,
        *,
        instance: RemoteInstance,
        envelope_dict: dict,
    ) -> _TransportSendResult:
        """Deliver ``envelope_dict`` to *instance*, RTC first, inbox on fallback.

        The envelope is unchanged across transports — the signature and
        AES-256-GCM payload are already baked in.
        """
        peer = self._peers.get(instance.id)
        if peer is not None and peer.is_ready:
            try:
                sent = await peer.send(envelope_dict)
            except Exception as exc:
                log.warning(
                    "fed RTC send to %s raised (%s) — falling back to HTTPS inbox",
                    instance.id,
                    exc,
                )
                sent = False
            if sent:
                return _TransportSendResult(ok=True, via="rtc")
            log.debug(
                "fed RTC send to %s not ready — falling back to HTTPS inbox",
                instance.id,
            )

        # Kick off (or re-kick) the handshake lazily on first use.
        if peer is None:
            await self._ensure_handshake(instance)

        ok, status = await self._https_inbox.send(
            instance=instance,
            envelope_dict=envelope_dict,
        )
        return _TransportSendResult(
            ok=ok,
            via="https",
            status_code=status,
            error=None if ok else "https_inbox_failed",
        )

    async def _ensure_handshake(self, instance: RemoteInstance) -> None:
        async with self._lock:
            if instance.id in self._peers:
                return
            peer = _RtcPeer(
                instance_id=instance.id,
                ice_servers=self._ice_servers,
                signaling=self._signaling_factory(instance.id),
                inbound=self._inbound_factory(instance.id),
                bus=self._bus,
                polite=self._own_instance_id > instance.id,
                on_failed=self._evict_peer,
            )
            self._peers[instance.id] = peer
        # Release lock before the network call — the signalling round
        # trip should not block peer-registry lookups on other tasks.
        try:
            await peer.start_offer()
        except Exception as exc:
            log.warning(
                "fed RTC handshake start failed for %s: %s",
                instance.id,
                exc,
            )

    def _signaling_factory(
        self,
        instance_id: str,
    ) -> Callable[[FederationEventType, dict], Awaitable[None]]:
        async def _signal(et: FederationEventType, payload: dict) -> None:
            await self._signaling_send(instance_id, et, payload)

        return _signal

    def _inbound_factory(self, instance_id: str) -> _InboundCallback:
        async def _on_inbound(envelope: dict) -> None:
            log.debug(
                "fed RTC frame received from %s (msg_id=%s, type=%s)",
                instance_id,
                envelope.get("msg_id"),
                envelope.get("event_type"),
            )
            # Feed inbound DataChannel frames through the same §24.11
            # validation pipeline the HTTPS-inbox path uses — but with the
            # instance resolved by instance_id (already known from the
            # peer connection) instead of inbox_id.
            if self._inbound_handler is not None:
                raw = orjson.dumps(envelope)
                try:
                    await self._inbound_handler(instance_id, raw)
                except ValueError as exc:
                    log.warning(
                        "fed RTC inbound rejected from %s: %s",
                        instance_id,
                        exc,
                    )

        return _on_inbound

    # ─── Inbound signalling ──────────────────────────────────────────────

    async def on_rtc_offer(
        self,
        *,
        from_instance: str,
        payload: dict,
    ) -> None:
        """Handle a ``FEDERATION_RTC_OFFER`` from a paired peer."""
        async with self._lock:
            peer = self._peers.get(from_instance)
            if peer is None:
                peer = _RtcPeer(
                    instance_id=from_instance,
                    ice_servers=self._ice_servers,
                    signaling=self._signaling_factory(from_instance),
                    inbound=self._inbound_factory(from_instance),
                    bus=self._bus,
                    polite=self._own_instance_id > from_instance,
                    on_failed=self._evict_peer,
                )
                self._peers[from_instance] = peer
        sdp = str(payload.get("sdp") or "")
        if not sdp:
            return
        await peer.accept_offer(sdp=sdp, from_instance=from_instance)

    async def on_rtc_answer(
        self,
        *,
        from_instance: str,
        payload: dict,
    ) -> None:
        """Handle a ``FEDERATION_RTC_ANSWER`` (S-14 origin-guarded)."""
        peer = self._peers.get(from_instance)
        if peer is None:
            log.warning(
                "RTC answer from %s ignored — no pending peer",
                from_instance,
            )
            return
        sdp = str(payload.get("sdp") or "")
        if not sdp:
            return
        await peer.apply_answer(sdp=sdp, from_instance=from_instance)

    async def on_rtc_ice(
        self,
        *,
        from_instance: str,
        payload: dict,
    ) -> None:
        """Handle a trickled ``FEDERATION_RTC_ICE`` candidate.

        Creates a buffering :class:`_RtcPeer` stub if no peer exists
        yet for ``from_instance`` so an ICE envelope that overtook the
        ``FEDERATION_RTC_OFFER`` doesn't end up silently dropped (which
        previously stranded ICE with no remote candidates, expiring
        the connectivity timer and failing the DataChannel). The stub
        peer just queues candidates inside
        :meth:`_RtcPeer.add_ice_candidate`; ``on_rtc_offer`` reuses the
        same dict slot, builds the PeerConnection, and once
        ``set_remote_description`` lands the buffered candidates flush
        in :meth:`_RtcPeer.add_ice_candidate`. The §24.11 pipeline has
        already authenticated the sender by signature, so the stub
        peer can't be a DoS vector against unpaired instance_ids.
        """
        async with self._lock:
            peer = self._peers.get(from_instance)
            if peer is None:
                peer = _RtcPeer(
                    instance_id=from_instance,
                    ice_servers=self._ice_servers,
                    signaling=self._signaling_factory(from_instance),
                    inbound=self._inbound_factory(from_instance),
                    bus=self._bus,
                    polite=self._own_instance_id > from_instance,
                    on_failed=self._evict_peer,
                )
                self._peers[from_instance] = peer
        await peer.add_ice_candidate(
            candidate=str(payload.get("candidate") or ""),
            sdp_mid=str(payload.get("sdp_mid") or "0"),
        )

    async def _evict_peer(self, instance_id: str) -> None:
        """Drop a ``_RtcPeer`` after its underlying PC entered FAILED.

        Without this hook a failed PC stayed in ``_peers`` forever and
        every subsequent ``send()`` short-circuited to HTTPS fallback
        without trying to rebuild the channel. Removing the entry lets
        the next outbound envelope call ``_ensure_handshake`` again.

        Bound to ``_RtcPeer(on_failed=...)`` at instantiation.
        """
        async with self._lock:
            peer = self._peers.pop(instance_id, None)
        if peer is None:
            return
        log.info(
            "fed RTC peer %s evicted after FAILED state; next send will "
            "rebuild the handshake",
            instance_id,
        )
        try:
            peer.close()
        except Exception as exc:  # noqa: BLE001 — defensive on shutdown path
            log.debug("fed RTC peer %s close after evict: %s", instance_id, exc)

    # ─── Inspection + shutdown ───────────────────────────────────────────

    def is_ready(self, instance_id: str) -> bool:
        peer = self._peers.get(instance_id)
        return peer is not None and peer.is_ready

    def peer_count(self) -> int:
        return len(self._peers)

    async def close_peer(self, instance_id: str) -> None:
        peer = self._peers.pop(instance_id, None)
        if peer is not None:
            peer.close()

    async def close_all(self) -> None:
        for peer in list(self._peers.values()):
            peer.close()
        self._peers.clear()
