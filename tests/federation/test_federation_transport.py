"""Tests for FederationTransport (§24.12.5).

The test conftest injects a fake ``aiolibdatachannel`` module into
``sys.modules`` before any production imports, so the DataChannel
state machine uses deterministic fake objects. The peer ``is_ready``
flag is flipped explicitly by marking ``_open`` / ``_closed`` on
``_RtcPeer``. That's enough to exercise the facade's primary /
fallback branches without the native binding.
"""

from __future__ import annotations

import asyncio

import aiolibdatachannel as rtc

from socialhome.domain.events import PeerTransportChanged
from socialhome.domain.federation import (
    DeliveryResult,
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.transport import (
    FederationTransport,
    HttpsInboxTransport,
    _RtcPeer,
)
from socialhome.infrastructure.event_bus import EventBus


# ─── Fakes ────────────────────────────────────────────────────────────────


def _fake_instance(iid: str = "peer-1") -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="https://peer/wh",
        local_inbox_id=f"wh-{iid}",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )


class _RecordingHttpsInbox:
    """Drop-in replacement for :class:`HttpsInboxTransport` used by facade tests."""

    def __init__(self, *, ok: bool = True, status: int | None = 200) -> None:
        self.ok = ok
        self.status = status
        self.calls: list[tuple[RemoteInstance, dict]] = []

    async def send(self, *, instance, envelope_dict):
        self.calls.append((instance, envelope_dict))
        return self.ok, self.status


class _FakeSignaler:
    """Captures :meth:`FederationTransport.send` signalling round-trips."""

    def __init__(self):
        self.events: list[tuple[str, FederationEventType, dict]] = []

    async def __call__(self, to_instance_id, event_type, payload):
        self.events.append((to_instance_id, event_type, payload))
        return DeliveryResult(
            instance_id=to_instance_id,
            ok=True,
            status_code=200,
        )


# ─── Facade: primary + fallback + handshake ───────────────────────────────


async def test_send_uses_rtc_when_peer_is_ready():
    """A peer whose DataChannel is already open takes the RTC path."""
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    inst = _fake_instance("peer-1")

    # Synthesise a ready peer (stub mode never opens the channel
    # on its own).
    peer = _RtcPeer(
        instance_id=inst.id,
        ice_servers=None,
        signaling=t._signaling_factory(inst.id),
        inbound=t._inbound_factory(inst.id),
    )

    # Mark the peer ready + attach a fake channel that records sends.
    class _FakeChannel:
        def __init__(self):
            self.sent = []
            self.buffered_amount = 0

        async def send(self, data):
            self.sent.append(data)

    fake_ch = _FakeChannel()
    peer._channel = fake_ch  # type: ignore[attr-defined]
    peer._open.set()  # type: ignore[attr-defined]
    t._peers[inst.id] = peer  # type: ignore[attr-defined]

    result = await t.send(instance=inst, envelope_dict={"msg_id": "x"})

    assert result.ok is True
    assert result.via == "rtc"
    assert fake_ch.sent  # DataChannel received the frame
    assert https_inbox.calls == []  # no HTTPS https_inbox fallback


async def test_send_falls_back_to_inbox_when_peer_not_ready():
    """No RTC channel yet → facade starts a handshake AND uses HTTPS https_inbox."""
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    inst = _fake_instance("peer-2")

    result = await t.send(instance=inst, envelope_dict={"msg_id": "x"})

    assert result.ok is True
    assert result.via == "https"
    assert len(https_inbox.calls) == 1
    # Handshake was kicked — one OFFER was sent through the signaler.
    assert (
        signal.events
        and signal.events[0][1] is FederationEventType.FEDERATION_RTC_OFFER
    )


async def test_send_falls_back_when_inbox_fails():
    """HTTPS https_inbox returning non-2xx bubbles up as ``ok=False``."""
    https_inbox = _RecordingHttpsInbox(ok=False, status=502)
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    result = await t.send(
        instance=_fake_instance("peer-3"),
        envelope_dict={"msg_id": "x"},
    )
    assert result.ok is False
    assert result.via == "https"
    assert result.status_code == 502


async def test_send_falls_back_to_inbox_when_rtc_send_raises():
    """An RTC send that errors is swallowed; https_inbox delivers instead."""
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    inst = _fake_instance("peer-4")

    class _RaisingPeer(_RtcPeer):
        """Subclass because ``_RtcPeer`` uses ``__slots__`` — we can't
        patch ``.send`` on an instance, so override at the class level.
        """

        @property
        def is_ready(self) -> bool:
            return True

        async def send(self, envelope_dict):
            raise RuntimeError("boom")

    peer = _RaisingPeer(
        instance_id=inst.id,
        ice_servers=None,
        signaling=t._signaling_factory(inst.id),
        inbound=t._inbound_factory(inst.id),
    )
    t._peers[inst.id] = peer

    result = await t.send(instance=inst, envelope_dict={"msg_id": "x"})
    assert result.ok is True
    assert result.via == "https"
    assert https_inbox.calls


# ─── Inbound signalling ────────────────────────────────────────────────────


async def test_on_rtc_offer_creates_peer_and_sends_answer():
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    await t.on_rtc_offer(
        from_instance="peer-5",
        payload={"sdp": "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\n", "sdp_type": "offer"},
    )
    assert "peer-5" in t._peers
    # Answerer posted a FEDERATION_RTC_ANSWER back through the signaler.
    assert any(
        ev[1] is FederationEventType.FEDERATION_RTC_ANSWER for ev in signal.events
    )


async def test_on_rtc_offer_ignores_empty_sdp():
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    await t.on_rtc_offer(from_instance="peer-6", payload={"sdp": ""})
    # Peer was still registered (we hold the slot) but no ANSWER sent.
    assert not any(
        ev[1] is FederationEventType.FEDERATION_RTC_ANSWER for ev in signal.events
    )


async def test_on_rtc_answer_with_matching_from_applies():
    """S-14: the answer origin must match the pending-offer target."""
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    inst = _fake_instance("peer-7")
    # Prime the peer with a pending offer.
    await t._ensure_handshake(inst)

    await t.on_rtc_answer(
        from_instance="peer-7",
        payload={"sdp": "answer-sdp", "sdp_type": "answer"},
    )
    peer = t._peers["peer-7"]
    assert peer._expected_answer_from is None  # type: ignore[attr-defined]


async def test_on_rtc_answer_with_mismatched_from_is_rejected():
    """S-14: an answer from the wrong peer must NOT be applied."""
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    inst = _fake_instance("peer-8")
    await t._ensure_handshake(inst)

    await t.on_rtc_answer(
        from_instance="attacker",
        payload={"sdp": "evil", "sdp_type": "answer"},
    )
    peer = t._peers["peer-8"]
    # Still expecting the real peer's answer.
    assert peer._expected_answer_from == "peer-8"  # type: ignore[attr-defined]


async def test_on_rtc_answer_unknown_peer_is_noop():
    """Answer for a peer we never offered to is dropped silently."""
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    await t.on_rtc_answer(
        from_instance="ghost",
        payload={"sdp": "x"},
    )
    assert t.peer_count() == 0


async def test_on_rtc_ice_unknown_peer_creates_buffering_stub(monkeypatch):
    """ICE arriving before the matching OFFER is buffered in a stub
    :class:`_RtcPeer` so a later OFFER can flush it — previously the
    candidate was silently dropped, stranding ICE with no remote
    candidates and failing the WebRTC connectivity timer."""
    from socialhome.federation import transport as transport_mod

    # Short timeout so the buffered candidate doesn't park 10s and
    # leave a lingering task at teardown.
    monkeypatch.setattr(transport_mod, "ICE_BUFFER_TIMEOUT_S", 0.05)

    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    # Should not raise — stub peer is created and buffers the candidate.
    await t.on_rtc_ice(
        from_instance="ghost",
        payload={"candidate": "c", "sdp_mid": "0"},
    )
    # The stub peer is registered so a follow-up OFFER will reuse it.
    assert "ghost" in t._peers


async def test_on_rtc_ice_accepts_trickled_candidate():
    """A candidate arriving after the remote description has been applied
    flushes straight through to the PC. Uses a stub peer to avoid
    spinning up a real aiolibdatachannel PeerConnection (whose
    iterator tasks the pytest_homeassistant_custom_component plugin
    would flag at teardown)."""
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )

    applied: list[tuple[str, str]] = []

    class _StubPc:
        async def add_remote_candidate(self, candidate, sdp_mid):
            applied.append((candidate, sdp_mid))

    # Pre-seed a stub peer that pretends its remote description is
    # already in. ``on_rtc_ice`` will find it in ``_peers`` and forward
    # the candidate directly.
    peer = _RtcPeer(
        instance_id="peer-9",
        ice_servers=None,
        signaling=t._signaling_factory("peer-9"),
        inbound=t._inbound_factory("peer-9"),
    )
    peer._pc = _StubPc()
    peer._remote_description_applied.set()
    t._peers["peer-9"] = peer

    await t.on_rtc_ice(
        from_instance="peer-9",
        payload={
            "candidate": "candidate:1 udp 1 1.1.1.1 5000 typ host",
            "sdp_mid": "0",
        },
    )
    assert applied == [("candidate:1 udp 1 1.1.1.1 5000 typ host", "0")]


# ─── Wire-ordering invariant: SDP before any trickled ICE ─────────────────
#
# Real-world regression: ``_drain_ice`` was spawned *before*
# :meth:`_RtcPeer.accept_offer` (and ``start_offer``) awaited the SDP
# signaling. The drain task and the SDP signaling were independent HTTPS
# posts to the peer's inbox, so a candidate POST could outrun the
# OFFER/ANSWER POST on the wire. The offerer (us) then dropped the
# candidate at :data:`ICE_BUFFER_TIMEOUT_S` because its remote description
# hadn't been applied yet, and ICE connectivity check failed with zero
# remote candidates. federation-demo missed it because loopback HTTPS
# latency is microseconds and the ANSWER always won the race.


class _RacingPeerConnection(rtc.PeerConnection):  # type: ignore[misc]
    """Fake PC whose ICE gathering emits a candidate the moment
    ``set_local_description`` starts — models libdatachannel surfacing
    host candidates immediately on the gathering pass, which is exactly
    when the race window opens.
    """

    def __init__(self, config=None) -> None:
        super().__init__(config)
        self._ice_queue: asyncio.Queue = asyncio.Queue()

    async def set_local_description(self, type_: str = "offer"):
        self._ice_queue.put_nowait(
            rtc.IceCandidate(
                "candidate:host 1 udp 1 1.1.1.1 5000 typ host",
                "0",
            ),
        )
        return await super().set_local_description(type_)

    async def ice_candidates(self):
        while not self._closed:
            cand = await self._ice_queue.get()
            if cand is None:
                return
            yield cand

    def close(self) -> None:
        try:
            self._ice_queue.put_nowait(None)
        except Exception:  # noqa: BLE001
            pass
        super().close()


async def _settle_drain(signal: "_FakeSignaler", *, attempts: int = 20) -> None:
    """Yield until the drain task has emitted at least one ICE event
    (or we run out of patience). Deterministic on the fake event loop —
    a handful of ``sleep(0)`` cycles is enough because the fake signaler
    completes synchronously."""
    for _ in range(attempts):
        if any(ev[1] is FederationEventType.FEDERATION_RTC_ICE for ev in signal.events):
            return
        await asyncio.sleep(0)


async def test_accept_offer_signals_answer_before_ice_candidates(monkeypatch):
    """Answerer side: ANSWER must hit the signaler before any ICE.

    If the drain task starts before ``_signaling(ANSWER, ...)`` is
    awaited, candidate POSTs can win the race to the offerer's inbox,
    and the offerer drops them at :data:`ICE_BUFFER_TIMEOUT_S` before
    its remote description has been applied.
    """
    monkeypatch.setattr(rtc, "PeerConnection", _RacingPeerConnection)

    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )

    await t.on_rtc_offer(
        from_instance="peer-race-a",
        payload={
            "sdp": "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\n",
            "sdp_type": "offer",
        },
    )
    await _settle_drain(signal)

    event_types = [ev[1] for ev in signal.events]
    answer_idx = event_types.index(FederationEventType.FEDERATION_RTC_ANSWER)
    ice_indices = [
        i
        for i, et in enumerate(event_types)
        if et is FederationEventType.FEDERATION_RTC_ICE
    ]
    assert ice_indices, "drain task should have flushed the host candidate"
    assert all(answer_idx < i for i in ice_indices), (
        f"wire order broken: ANSWER at {answer_idx}, "
        f"ICE candidates at {ice_indices} — the answerer raced ICE "
        "ahead of ANSWER (signaling race in _RtcPeer.accept_offer)"
    )

    await t.close_all()


async def test_start_offer_signals_offer_before_ice_candidates(monkeypatch):
    """Offerer side: OFFER must hit the signaler before any ICE.

    Symmetric to the answerer invariant — the receiver buffers ICE that
    overtakes an OFFER, but only inside :data:`ICE_BUFFER_TIMEOUT_S`,
    so out-of-order trickle on a slow link still fails.
    """
    monkeypatch.setattr(rtc, "PeerConnection", _RacingPeerConnection)

    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )

    await t._ensure_handshake(_fake_instance("peer-race-o"))
    await _settle_drain(signal)

    event_types = [ev[1] for ev in signal.events]
    offer_idx = event_types.index(FederationEventType.FEDERATION_RTC_OFFER)
    ice_indices = [
        i
        for i, et in enumerate(event_types)
        if et is FederationEventType.FEDERATION_RTC_ICE
    ]
    assert ice_indices, "drain task should have flushed the host candidate"
    assert all(offer_idx < i for i in ice_indices), (
        f"wire order broken: OFFER at {offer_idx}, "
        f"ICE candidates at {ice_indices} — the offerer raced ICE "
        "ahead of OFFER (signaling race in _RtcPeer.start_offer)"
    )

    await t.close_all()


# ─── Facade lifecycle ──────────────────────────────────────────────────────


async def test_close_peer_removes_entry():
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    await t._ensure_handshake(_fake_instance("peer-10"))
    assert t.peer_count() == 1
    await t.close_peer("peer-10")
    assert t.peer_count() == 0


async def test_close_all_drops_every_peer():
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    await t._ensure_handshake(_fake_instance("a"))
    await t._ensure_handshake(_fake_instance("b"))
    await t.close_all()
    assert t.peer_count() == 0


async def test_is_ready_reports_false_for_unknown_peer():
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    assert t.is_ready("never-seen") is False


# ─── HttpsInboxTransport ──────────────────────────────────────────────────────


async def test_https_inbox_transport_2xx_is_ok():
    """HttpsInboxTransport.send returns (True, status) for 2xx."""

    class _FakeResp:
        def __init__(self, status):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeClient:
        def post(self, url, json, timeout):
            return _FakeResp(204)

    async def _factory():
        return _FakeClient()

    wt = HttpsInboxTransport(client_factory=_factory)
    ok, status = await wt.send(
        instance=_fake_instance("peer"),
        envelope_dict={"msg_id": "x"},
    )
    assert ok is True and status == 204


async def test_https_inbox_transport_non_2xx_is_failure():
    class _FakeResp:
        def __init__(self, status):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeClient:
        def post(self, url, json, timeout):
            return _FakeResp(503)

    async def _factory():
        return _FakeClient()

    wt = HttpsInboxTransport(client_factory=_factory)
    ok, status = await wt.send(
        instance=_fake_instance("peer"),
        envelope_dict={"x": 1},
    )
    assert ok is False and status == 503


async def test_https_inbox_transport_network_error_is_failure():
    class _RaisingClient:
        def post(self, *a, **kw):
            raise RuntimeError("boom")

    async def _factory():
        return _RaisingClient()

    wt = HttpsInboxTransport(client_factory=_factory)
    ok, status = await wt.send(
        instance=_fake_instance("peer"),
        envelope_dict={"x": 1},
    )
    assert ok is False and status is None


# ─── PeerTransportChanged publication ─────────────────────────────────────


async def test_rtc_peer_publishes_transport_changed_on_open():
    """Opening the DataChannel publishes PeerTransportChanged(transport='rtc')."""
    bus = EventBus()
    received: list[PeerTransportChanged] = []

    async def _record(e: PeerTransportChanged) -> None:
        received.append(e)

    bus.subscribe(PeerTransportChanged, _record)

    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
        bus=bus,
    )

    peer = _RtcPeer(
        instance_id="peer-tx",
        ice_servers=None,
        signaling=t._signaling_factory("peer-tx"),
        inbound=t._inbound_factory("peer-tx"),
        bus=bus,
    )
    # Simulate the open edge — set the asyncio.Event the way
    # _drain_channel does once the channel reports OPEN.
    peer._open.set()
    await peer._publish_open_if_needed()

    assert len(received) == 1
    assert received[0].instance_id == "peer-tx"
    assert received[0].transport == "rtc"


async def test_rtc_peer_publishes_transport_changed_on_close():
    """Closing the peer (after a successful open) publishes
    PeerTransportChanged(transport='https')."""
    bus = EventBus()
    received: list[PeerTransportChanged] = []

    async def _record(e: PeerTransportChanged) -> None:
        received.append(e)

    bus.subscribe(PeerTransportChanged, _record)

    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
        bus=bus,
    )
    peer = _RtcPeer(
        instance_id="peer-cx",
        ice_servers=None,
        signaling=t._signaling_factory("peer-cx"),
        inbound=t._inbound_factory("peer-cx"),
        bus=bus,
    )
    peer._open.set()
    peer._loop = asyncio.get_running_loop()
    await peer._publish_open_if_needed()
    received.clear()

    peer.close()
    # close() schedules a task — yield to let it run.
    await asyncio.sleep(0)

    assert len(received) == 1
    assert received[0].instance_id == "peer-cx"
    assert received[0].transport == "https"


async def test_rtc_peer_does_not_publish_close_without_prior_open():
    """A peer that never opened doesn't publish a spurious 'https' on close.
    The transport never *flipped* — it was always HTTPS."""
    bus = EventBus()
    received: list[PeerTransportChanged] = []

    async def _record(e: PeerTransportChanged) -> None:
        received.append(e)

    bus.subscribe(PeerTransportChanged, _record)

    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
        bus=bus,
    )
    peer = _RtcPeer(
        instance_id="peer-stub",
        ice_servers=None,
        signaling=t._signaling_factory("peer-stub"),
        inbound=t._inbound_factory("peer-stub"),
        bus=bus,
    )
    peer.close()
    await asyncio.sleep(0)
    assert received == []


async def test_rtc_peer_close_is_idempotent():
    """A double close() after a prior open publishes 'https' exactly once,
    not twice — the second close() is a no-op for the bus."""
    bus = EventBus()
    received: list[PeerTransportChanged] = []

    async def _record(e: PeerTransportChanged) -> None:
        received.append(e)

    bus.subscribe(PeerTransportChanged, _record)

    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
        bus=bus,
    )
    peer = _RtcPeer(
        instance_id="peer-double-close",
        ice_servers=None,
        signaling=t._signaling_factory("peer-double-close"),
        inbound=t._inbound_factory("peer-double-close"),
        bus=bus,
    )
    peer._loop = asyncio.get_running_loop()
    peer._open.set()
    await peer._publish_open_if_needed()
    received.clear()

    peer.close()
    peer.close()  # second call — must not publish again

    # Let the create_task'd publishes settle.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(received) == 1
    assert received[0].instance_id == "peer-double-close"
    assert received[0].transport == "https"


# ─── Perfect negotiation (glare resolution) ───────────────────────────────
#
# When two paired peers both fire ``start_offer`` simultaneously, both
# sides POST an OFFER via HTTPS inbox. Without perfect negotiation both
# sides' ``accept_offer`` would clobber their own pending PeerConnection
# and ICE would never converge. The tests below pin the correct behaviour:
#
#  - impolite side (lex-smaller own_id > peer_id = False) ignores
#    the incoming OFFER and keeps its own pending offer alive.
#  - polite side (lex-smaller own_id > peer_id = True) rolls back its
#    pending offer and accepts the impolite peer's OFFER instead.
#  - After rollback a stale ANSWER (for the polite side's now-cancelled
#    offer) is silently dropped.
#  - The politeness role is symmetric and deterministic for any pair
#    of instance-ids.

_STUB_SDP = "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\na=mock\r\n"


async def _make_peer(
    own_id: str,
    peer_id: str,
    *,
    signal: _FakeSignaler | None = None,
) -> tuple[_RtcPeer, _FakeSignaler]:
    """Helper: create a ``_RtcPeer`` for *peer_id* as seen from *own_id*."""
    if signal is None:
        signal = _FakeSignaler()

    async def _signaling(et, payload):
        signal.events.append((peer_id, et, payload))
        return DeliveryResult(instance_id=peer_id, ok=True, status_code=200)

    async def _inbound(_envelope):
        return None

    peer = _RtcPeer(
        instance_id=peer_id,
        ice_servers=None,
        signaling=_signaling,
        inbound=_inbound,
        polite=own_id > peer_id,
    )
    return peer, signal


async def test_politeness_role_assignment():
    """Lex comparison is deterministic regardless of call-site perspective.

    If own_id="aaaa" and peer_id="bbbb":
      - A's perspective: own "aaaa" > peer "bbbb" = False → A is impolite
      - B's perspective: own "bbbb" > peer "aaaa" = True  → B is polite
    """
    peer_a_for_b, _ = await _make_peer("aaaa", "bbbb")  # A's _RtcPeer for B
    peer_b_for_a, _ = await _make_peer("bbbb", "aaaa")  # B's _RtcPeer for A

    assert peer_a_for_b._polite is False, "A (smaller id) should be impolite"
    assert peer_b_for_a._polite is True, "B (larger id) should be polite"


async def test_no_glare_baseline():
    """Only A initiates; B receives the OFFER and replies normally.

    Assert: A ends in have-local-offer (after start_offer), B ends in
    have-local-answer (after accept_offer), no rollback logged.
    """
    signal_a = _FakeSignaler()
    signal_b = _FakeSignaler()
    peer_a, _ = await _make_peer("aaaa", "bbbb", signal=signal_a)
    peer_b, _ = await _make_peer("bbbb", "aaaa", signal=signal_b)

    # A starts an offer.
    await peer_a.start_offer()
    assert peer_a._pc.signaling_state == "have-local-offer"  # type: ignore[union-attr]
    assert peer_a._making_offer is True

    # B receives A's offer and replies.
    offer_sdp = _STUB_SDP
    await peer_b.accept_offer(sdp=offer_sdp, from_instance="aaaa")
    assert peer_b._pc is not None
    assert peer_b._pc.signaling_state in ("have-local-answer", "stable")

    # Verify no rollback log: peer_b sent an ANSWER, not just silence.
    answer_events = [
        e for e in signal_b.events if e[1] is FederationEventType.FEDERATION_RTC_ANSWER
    ]
    assert answer_events, "B should have sent an ANSWER"

    # A applies B's answer.
    ok = await peer_a.apply_answer(sdp=_STUB_SDP, from_instance="bbbb")
    assert ok is True
    assert peer_a._making_offer is False

    peer_a.close()
    peer_b.close()


async def test_glare_impolite_ignores_incoming_offer():
    """Impolite side (own_id < peer_id) ignores an incoming OFFER while
    making its own offer.  The impolite PC stays in have-local-offer and
    no ANSWER is posted.
    """
    # A ("aaaa") is impolite: "aaaa" > "bbbb" is False.
    signal_a = _FakeSignaler()
    peer_a, _ = await _make_peer("aaaa", "bbbb", signal=signal_a)

    # A starts its own offer.
    await peer_a.start_offer()
    assert peer_a._making_offer is True
    assert peer_a._pc.signaling_state == "have-local-offer"  # type: ignore[union-attr]

    # B's OFFER arrives — impolite A should ignore it.
    await peer_a.accept_offer(sdp=_STUB_SDP, from_instance="bbbb")

    # A's PC must still be the original one (in have-local-offer state).
    assert peer_a._pc is not None
    assert peer_a._pc.signaling_state == "have-local-offer"  # type: ignore[union-attr]
    assert peer_a._making_offer is True  # still in "making offer" window

    # A must NOT have sent an ANSWER.
    answer_events = [
        e for e in signal_a.events if e[1] is FederationEventType.FEDERATION_RTC_ANSWER
    ]
    assert not answer_events, "Impolite side must not send an ANSWER on glare"

    peer_a.close()


async def test_glare_polite_side_rolls_back_and_accepts():
    """Polite side (own_id > peer_id) rolls back its pending offer and
    accepts the impolite peer's incoming OFFER.

    Sequence:
      1. B starts an offer (B is polite: "bbbb" > "aaaa").
      2. B receives A's OFFER while its own is pending → rollback.
      3. B builds a fresh answerer PC and sends an ANSWER.
    """
    signal_b = _FakeSignaler()
    peer_b, _ = await _make_peer("bbbb", "aaaa", signal=signal_b)

    # B starts its own offer.
    await peer_b.start_offer()
    original_pc = peer_b._pc
    assert peer_b._making_offer is True
    assert original_pc.signaling_state == "have-local-offer"  # type: ignore[union-attr]

    # A's OFFER arrives while B is making an offer → polite rollback.
    await peer_b.accept_offer(sdp=_STUB_SDP, from_instance="aaaa")

    # B must have built a NEW PC (the original was closed on rollback).
    assert peer_b._pc is not original_pc, "Polite side must create a fresh PC"
    assert peer_b._making_offer is False, "making_offer must be cleared after rollback"

    # B must have sent an ANSWER.
    answer_events = [
        e for e in signal_b.events if e[1] is FederationEventType.FEDERATION_RTC_ANSWER
    ]
    assert answer_events, "Polite side must send an ANSWER after rollback"

    peer_b.close()


async def test_late_answer_after_rollback_is_ignored():
    """After polite rollback, a stale ANSWER arriving for the cancelled
    offer is silently dropped — no exception, no state corruption.
    """
    signal_b = _FakeSignaler()
    peer_b, _ = await _make_peer("bbbb", "aaaa", signal=signal_b)

    # B starts offer, then rolls back by accepting A's offer.
    await peer_b.start_offer()
    await peer_b.accept_offer(sdp=_STUB_SDP, from_instance="aaaa")

    # Now B is in answerer mode; a late ANSWER for B's cancelled offer
    # arrives.  The PC is no longer in have-local-offer → ignored.
    ok = await peer_b.apply_answer(sdp=_STUB_SDP, from_instance="aaaa")
    assert ok is False, "Stale ANSWER after rollback must be ignored"

    peer_b.close()


async def test_full_glare_resolution_end_to_end():
    """Full glare scenario: A and B both call start_offer simultaneously.

    A ("aaaa") is impolite; B ("bbbb") is polite.

    Expected outcome:
      - A ignores B's OFFER (impolite path).
      - B rolls back and sends ANSWER for A's OFFER (polite path).
      - A applies B's ANSWER; ICE converges on one OFFER→ANSWER cycle.
      - Only one ANSWER is delivered end-to-end.
    """
    signal_a = _FakeSignaler()
    signal_b = _FakeSignaler()
    peer_a, _ = await _make_peer("aaaa", "bbbb", signal=signal_a)
    peer_b, _ = await _make_peer("bbbb", "aaaa", signal=signal_b)

    # Both sides fire start_offer "simultaneously".
    await peer_a.start_offer()
    await peer_b.start_offer()

    # Cross-deliver the offers.
    await peer_a.accept_offer(sdp=_STUB_SDP, from_instance="bbbb")  # A ignores
    await peer_b.accept_offer(sdp=_STUB_SDP, from_instance="aaaa")  # B rolls back

    # A's PC must still be the original offerer (have-local-offer).
    assert peer_a._pc is not None
    assert peer_a._pc.signaling_state == "have-local-offer"  # type: ignore[union-attr]
    assert peer_a._making_offer is True

    # B must have sent exactly one ANSWER.
    b_answers = [
        e for e in signal_b.events if e[1] is FederationEventType.FEDERATION_RTC_ANSWER
    ]
    assert len(b_answers) == 1, (
        f"Expected exactly one ANSWER from B, got {len(b_answers)}"
    )

    # A applies B's ANSWER.
    ok = await peer_a.apply_answer(sdp=_STUB_SDP, from_instance="bbbb")
    assert ok is True
    assert peer_a._making_offer is False

    # No ANSWER from A (impolite; kept its own offer).
    a_answers = [
        e for e in signal_a.events if e[1] is FederationEventType.FEDERATION_RTC_ANSWER
    ]
    assert not a_answers, "Impolite side must never send an ANSWER on glare"

    peer_a.close()
    peer_b.close()


async def test_no_glare_on_subsequent_send():
    """After pairing, a second ``transport.send`` does NOT re-trigger
    ``_ensure_handshake`` — the peer is already in ``_peers``.
    """
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    inst = _fake_instance("peer-ng")

    # First send: no peer yet → creates peer + starts handshake.
    await t.send(instance=inst, envelope_dict={"msg_id": "1"})
    offer_count_after_first = sum(
        1 for e in signal.events if e[1] is FederationEventType.FEDERATION_RTC_OFFER
    )
    assert offer_count_after_first == 1, "First send must trigger exactly one OFFER"
    assert "peer-ng" in t._peers

    # Second send: peer already registered → no new handshake, no new OFFER.
    await t.send(instance=inst, envelope_dict={"msg_id": "2"})
    offer_count_after_second = sum(
        1 for e in signal.events if e[1] is FederationEventType.FEDERATION_RTC_OFFER
    )
    assert offer_count_after_second == 1, "Second send must NOT trigger another OFFER"
