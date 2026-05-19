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
