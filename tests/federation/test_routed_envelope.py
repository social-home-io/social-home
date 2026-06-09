"""Tests for :class:`SpaceRoutedHandler` (§D2 PR 2 / v_6 mesh).

Covers:

* Send-then-relay-then-unwrap: an envelope shipped along [a, b, c]
  emerges as a synthesised :class:`FederationEvent` at c, with
  ``from_instance == a`` and ``routed_path == [a, b, c]``. The
  relay sees only the sealed ciphertext, never the plaintext.
* Reply leg: target ships an ACK via :meth:`send_routed_reply`; the
  origin unseals it and dispatches the inner event.
* Loop prevention: a relay drops an envelope whose ``route_id`` it
  has already seen.
* Cycle detection: an envelope whose forward path contains self
  beyond ``position+1`` is dropped.
* Wrong-next-hop: an envelope where ``path[position+1] != self`` is
  dropped (defensive).
* Missing target_eph_priv at unwrap → log + drop.
* Unknown direction → log + drop.
* send_routed validation: empty path / wrong origin raises.
* Defensive drops: missing route_id / path / position / non-dict
  sealed all dropped silently.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    PairingStatus,
)
from socialhome.federation import routed_crypto
from socialhome.federation.routed_envelope import SpaceRoutedHandler


# ── Test doubles ──────────────────────────────────────────────────────


@dataclass
class _FakeInstance:
    id: str
    status: PairingStatus = PairingStatus.CONFIRMED
    proto_version: int = 6


class _FakeFederationRepo:
    def __init__(self, instances: dict[str, _FakeInstance] | None = None) -> None:
        self._instances = instances or {}

    async def get_instance(self, instance_id: str) -> _FakeInstance | None:
        return self._instances.get(instance_id)


class _LinkedFederationService:
    """In-memory link between nodes — ``send_event`` ships straight
    into the destination node's :class:`SpaceRoutedHandler`.
    """

    def __init__(self, *, own_instance_id: str) -> None:
        self._own_instance_id = own_instance_id
        self.sent: list[dict[str, Any]] = []
        self.peers: dict[str, SpaceRoutedHandler] = {}

    @property
    def own_instance_id(self) -> str:
        return self._own_instance_id

    async def peer_supports(self, instance_id: str, *, min_version: int) -> bool:
        return True

    async def send_event(
        self,
        *,
        to_instance_id: str,
        event_type: FederationEventType,
        payload: dict,
        space_id: str | None = None,
    ) -> Any:
        self.sent.append(
            {
                "to": to_instance_id,
                "event_type": event_type,
                "payload": payload,
            }
        )
        dest = self.peers.get(to_instance_id)
        if dest is None or event_type is not FederationEventType.SPACE_ROUTED:
            return SimpleNamespace(ok=False, instance_id=to_instance_id)
        ev = FederationEvent(
            msg_id=f"m-{len(self.sent)}",
            event_type=event_type,
            from_instance=self._own_instance_id,
            to_instance=to_instance_id,
            timestamp="2026-05-22T00:00:00Z",
            payload=payload,
        )

        # Fire-and-forget to mirror real transport.
        async def _deliver() -> None:
            await dest._on_routed(ev)

        asyncio.create_task(_deliver())
        return SimpleNamespace(ok=True, instance_id=to_instance_id)


@dataclass
class _Node:
    instance_id: str
    fed: _LinkedFederationService
    repo: _FakeFederationRepo
    handler: SpaceRoutedHandler
    dispatched: list[FederationEvent]
    #: ``target_eph_pk_b64 → target_eph_priv_b64`` — populated by the
    #: test driver when the test mints an ephemeral *for* this node;
    #: the handler's ``target_eph_lookup`` reads it on unwrap.
    target_eph_store: dict[str, str]


def _build_chain(node_ids: list[str]) -> dict[str, _Node]:
    """Build a chain of nodes wired against each other.

    Each node's :class:`SpaceRoutedHandler` ships to other nodes'
    handlers via ``_LinkedFederationService``. The handler's
    ``target_eph_lookup`` reads from each node's ``target_eph_store``
    — the test mints an ephemeral for the target node and seeds the
    pub → priv mapping before shipping ``send_routed``.
    """
    nodes: dict[str, _Node] = {}
    for node_id in node_ids:
        dispatched: list[FederationEvent] = []
        target_eph_store: dict[str, str] = {}

        async def _dispatcher(
            ev: FederationEvent,
            captured: list[FederationEvent] = dispatched,
        ) -> None:
            captured.append(ev)

        def _lookup(pub: str, store: dict[str, str] = target_eph_store) -> str | None:
            return store.get(pub)

        fed = _LinkedFederationService(own_instance_id=node_id)
        repo = _FakeFederationRepo()
        handler = SpaceRoutedHandler(
            federation_service=fed,  # type: ignore[arg-type]
            federation_repo=repo,  # type: ignore[arg-type]
            event_dispatcher=_dispatcher,
            target_eph_lookup=_lookup,
        )
        nodes[node_id] = _Node(
            instance_id=node_id,
            fed=fed,
            repo=repo,
            handler=handler,
            dispatched=dispatched,
            target_eph_store=target_eph_store,
        )
    # Wire peers.
    for node_id, node in nodes.items():
        for other_id, other in nodes.items():
            if other_id == node_id:
                continue
            node.fed.peers[other_id] = other.handler
            node.repo._instances[other_id] = _FakeInstance(id=other_id)
    return nodes


def _mint_target_eph(node: _Node) -> str:
    """Mint a fresh ephemeral keypair for ``node`` (as if its
    :class:`RouteDiscoveryService` had answered a FIND_ROUTE) and
    seed the priv into its target_eph_store. Returns the pub b64."""
    priv, pub = routed_crypto.generate_ephemeral_keypair()
    node.target_eph_store[pub] = priv
    return pub


# ── Tests ─────────────────────────────────────────────────────────────


async def test_send_relay_unwrap_dispatches_with_origin_attribution():
    """A SPACE_ROUTED envelope shipped from a along [a, b, c] is
    relayed by b (without seeing plaintext) and unwrapped at c — c's
    dispatcher sees the inner event with ``from_instance == 'a'``
    (the origin), ``routed_path == [a, b, c]``, and
    ``routed_route_id`` set so the reply leg can correlate.
    """
    nodes = _build_chain(["a", "b", "c"])
    target_eph_pk = _mint_target_eph(nodes["c"])
    inner_payload = {"redeem_nonce": "n1", "invite_token": "tkn"}
    route_id = await nodes["a"].handler.send_routed(
        path=["a", "b", "c"],
        target_eph_pk_b64=target_eph_pk,
        inner_event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
        inner_payload=inner_payload,
    )
    assert route_id
    # Yield to the asyncio loop so the chained deliveries fire.
    for _ in range(8):
        await asyncio.sleep(0)
    # a shipped to b
    a_to_b = [s for s in nodes["a"].fed.sent if s["to"] == "b"]
    assert len(a_to_b) == 1
    # The relay never sees plaintext: the on-wire ``sealed`` blob
    # bears no resemblance to the original payload.
    a_payload = a_to_b[0]["payload"]
    assert "sealed" in a_payload
    assert "inner_payload" not in a_payload  # no plaintext leak
    sealed_json = json.dumps(a_payload["sealed"], sort_keys=True)
    assert "redeem_nonce" not in sealed_json
    assert "tkn" not in sealed_json
    # b shipped to c (position bumped from 0 to 1), propagating the
    # sealed blob unchanged.
    fwds_from_b = [s for s in nodes["b"].fed.sent if s["to"] == "c"]
    assert len(fwds_from_b) == 1
    assert fwds_from_b[0]["payload"]["position"] == 1
    assert fwds_from_b[0]["payload"]["direction"] == "forward"
    assert fwds_from_b[0]["payload"]["sealed"] == a_payload["sealed"]
    # c dispatched the inner event with origin attribution.
    assert len(nodes["c"].dispatched) == 1
    inner = nodes["c"].dispatched[0]
    assert inner.event_type is FederationEventType.SPACE_INVITE_TOKEN_REDEEM
    assert inner.from_instance == "a"
    assert inner.to_instance == "c"
    assert inner.payload == inner_payload
    assert inner.routed_path == ["a", "b", "c"]
    assert inner.routed_route_id == route_id


async def test_reply_leg_seals_back_to_origin():
    """After the forward leg lands at the target, the target ships a
    reply via :meth:`send_routed_reply` — the origin unseals it and
    dispatches the inner ACK event. The relay still only sees
    ciphertext."""
    nodes = _build_chain(["a", "b", "c"])
    target_eph_pk = _mint_target_eph(nodes["c"])
    await nodes["a"].handler.send_routed(
        path=["a", "b", "c"],
        target_eph_pk_b64=target_eph_pk,
        inner_event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
        inner_payload={"redeem_nonce": "n1", "invite_token": "tkn"},
    )
    for _ in range(8):
        await asyncio.sleep(0)
    forward = nodes["c"].dispatched[0]
    route_id = forward.routed_route_id
    assert route_id is not None
    # Target ships the ACK back via send_routed_reply.
    ack_payload = {"redeem_nonce": "n1", "space_id": "sp-1", "role": "member"}
    await nodes["c"].handler.send_routed_reply(
        route_id=route_id,
        inner_event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
        inner_payload=ack_payload,
    )
    for _ in range(8):
        await asyncio.sleep(0)
    # c shipped a SPACE_ROUTED to b with direction=reply.
    c_to_b = [
        s
        for s in nodes["c"].fed.sent
        if s["to"] == "b" and s["event_type"] is FederationEventType.SPACE_ROUTED
    ]
    assert len(c_to_b) == 1
    assert c_to_b[0]["payload"]["direction"] == "reply"
    assert c_to_b[0]["payload"]["path"] == ["c", "b", "a"]
    # Sealed blob doesn't leak the ack plaintext.
    sealed_json = json.dumps(c_to_b[0]["payload"]["sealed"], sort_keys=True)
    assert "sp-1" not in sealed_json
    assert "member" not in sealed_json
    # a dispatched the unsealed ACK.
    ack_events = [
        e
        for e in nodes["a"].dispatched
        if e.event_type is FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK
    ]
    assert len(ack_events) == 1
    assert ack_events[0].payload == ack_payload
    assert ack_events[0].from_instance == "c"
    assert ack_events[0].routed_path == ["c", "b", "a"]


async def test_send_routed_reply_without_state_raises():
    """A reply for an unknown route_id (never received a forward leg
    or state expired) raises :class:`LookupError`."""
    nodes = _build_chain(["a"])
    with pytest.raises(LookupError):
        await nodes["a"].handler.send_routed_reply(
            route_id="never-seen",
            inner_event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
            inner_payload={},
        )


async def test_loop_detection_drops_duplicate_route_id():
    """A relay that has already seen ``route_id`` drops the inbound."""
    nodes = _build_chain(["a", "b"])
    handler_b = nodes["b"].handler
    handler_b._seen_routes["rid-dupe"] = float("inf")
    nodes["b"].fed.sent.clear()
    ev = FederationEvent(
        msg_id="m-dupe",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "rid-dupe",
            "path": ["a", "b", "x"],
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"kem_suite": "x25519", "ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].fed.sent == []
    assert nodes["b"].dispatched == []


async def test_cycle_detection_drops_self_in_forward_path():
    """An envelope whose forward portion of ``path`` (after
    ``position+1``) contains self → drop. Discovery should never
    produce this, but defensively the relay catches it."""
    nodes = _build_chain(["a", "b", "c"])
    handler_b = nodes["b"].handler
    nodes["b"].fed.sent.clear()
    ev = FederationEvent(
        msg_id="m-cycle",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "rid-cycle",
            "path": ["a", "b", "c", "b"],  # b reappears after position+1
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"kem_suite": "x25519", "ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].fed.sent == []


async def test_wrong_next_hop_is_dropped():
    """An envelope where ``path[position+1] != self`` is dropped —
    we're not actually the next hop, so forwarding would be incorrect.
    """
    nodes = _build_chain(["a", "b", "c"])
    handler_b = nodes["b"].handler
    nodes["b"].fed.sent.clear()
    ev = FederationEvent(
        msg_id="m-wrong",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            # path[position+1] should be 'b' to reach us; we put 'x'.
            "route_id": "rid-wrong",
            "path": ["a", "x", "c"],
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"kem_suite": "x25519", "ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].fed.sent == []
    assert nodes["b"].dispatched == []


async def test_overlong_path_is_dropped():
    """A SPACE_ROUTED ``path`` longer than the hard cap is dropped before
    forwarding — a relay trusts the path it's handed, so an over-long chain
    (beyond what discovery can produce) must not be re-fanned."""
    nodes = _build_chain(["a", "b", "c"])
    handler_b = nodes["b"].handler
    nodes["b"].fed.sent.clear()
    nodes["b"].dispatched.clear()
    # 9 distinct nodes > _MAX_ROUTED_PATH_LEN (8); we're at position 0.
    long_path = ["a", "b"] + [f"n{i}" for i in range(7)]
    ev = FederationEvent(
        msg_id="m-long",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "rid-long",
            "path": long_path,
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"kem_suite": "x25519", "ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].fed.sent == []
    assert nodes["b"].dispatched == []


async def test_path_position_sender_mismatch_is_dropped():
    """The previous hop named at ``path[position]`` must be the authenticated
    sender; a mismatch (mis-route / off-path replay) is dropped, not forwarded.
    """
    nodes = _build_chain(["a", "b", "c"])
    handler_b = nodes["b"].handler
    nodes["b"].fed.sent.clear()
    nodes["b"].dispatched.clear()
    ev = FederationEvent(
        msg_id="m-mismatch",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="z",  # NOT path[position]=='a'
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "rid-mismatch",
            "path": ["a", "b", "c"],
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"kem_suite": "x25519", "ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].fed.sent == []
    assert nodes["b"].dispatched == []


async def test_send_routed_rejects_short_path():
    """`send_routed` raises on a one-element path — not a route."""
    nodes = _build_chain(["a"])
    pub = _mint_target_eph(nodes["a"])
    with pytest.raises(ValueError, match="at least origin and target"):
        await nodes["a"].handler.send_routed(
            path=["a"],
            target_eph_pk_b64=pub,
            inner_event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
            inner_payload={},
        )


async def test_send_routed_rejects_path_with_wrong_origin():
    """`send_routed` raises when ``path[0]`` is not our own instance."""
    nodes = _build_chain(["a", "b"])
    pub = _mint_target_eph(nodes["b"])
    with pytest.raises(ValueError, match="path\\[0\\]"):
        await nodes["a"].handler.send_routed(
            path=["x", "b"],
            target_eph_pk_b64=pub,
            inner_event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
            inner_payload={},
        )


async def test_missing_route_id_is_dropped():
    """Envelope lacking ``route_id`` is dropped silently."""
    nodes = _build_chain(["b"])
    handler_b = nodes["b"].handler
    ev = FederationEvent(
        msg_id="m-no-rid",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "path": ["a", "b"],
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []


async def test_missing_path_is_dropped():
    """Envelope lacking ``path`` is dropped silently."""
    nodes = _build_chain(["b"])
    handler_b = nodes["b"].handler
    ev = FederationEvent(
        msg_id="m-no-path",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "r1",
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []


async def test_non_dict_sealed_is_dropped():
    """Envelope with a non-dict ``sealed`` blob is dropped."""
    nodes = _build_chain(["a", "b"])
    handler_b = nodes["b"].handler
    ev = FederationEvent(
        msg_id="m-bad-sealed",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "r1",
            "path": ["a", "b"],
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": "not a dict",
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []


async def test_unknown_inner_event_type_is_dropped():
    """An envelope wrapping an unknown event_type is dropped — we
    can't synthesise a :class:`FederationEvent` for a value not in
    the enum.
    """
    nodes = _build_chain(["a", "b"])
    handler_b = nodes["b"].handler
    ev = FederationEvent(
        msg_id="m-unknown",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "r1",
            "path": ["a", "b"],
            "position": 0,
            "direction": "forward",
            "inner_event_type": "future_event_we_dont_know",
            "sealed": {"ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []


async def test_unknown_direction_is_dropped():
    """An envelope with ``direction`` outside the {forward, reply}
    set is dropped — a future direction must arrive in a build that
    understands it, never on a build that would just guess."""
    nodes = _build_chain(["a", "b"])
    handler_b = nodes["b"].handler
    ev = FederationEvent(
        msg_id="m-bad-dir",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "r-bad-dir",
            "path": ["a", "b"],
            "position": 0,
            "direction": "sideways",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []


async def test_missing_target_eph_priv_at_unwrap_drops():
    """When the inbound forward leg lands on the target but no cached
    target ephemeral priv matches ``sealed.target_eph_pk`` (expired /
    unknown), the unwrap step drops + logs rather than crashing."""
    nodes = _build_chain(["a", "b"])
    # NOTE: we deliberately do NOT seed nodes["b"].target_eph_store
    # — the lookup returns None and the unwrap should drop.
    bogus_pub = _mint_target_eph(nodes["a"])  # b's store stays empty
    # Use the real seal machinery so JSON parse + unseal logic flows
    # through, but with the wrong target so the lookup fails first.
    inner_payload_json = '{"x":1}'
    o_priv, o_pub = routed_crypto.generate_ephemeral_keypair()
    sealed = routed_crypto.seal_inner_payload(
        inner_payload_json=inner_payload_json,
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=bogus_pub,
        route_id="r-missing",
        inner_event_type="space_invite_token_redeem",
    )
    ev = FederationEvent(
        msg_id="m-miss",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "r-missing",
            "path": ["a", "b"],
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": sealed,
        },
    )
    await nodes["b"].handler._on_routed(ev)
    assert nodes["b"].dispatched == []


async def test_attach_to_registers_handler():
    """`attach_to` registers the SPACE_ROUTED handler."""

    class _FakeRegistry:
        def __init__(self) -> None:
            self.bindings: dict[FederationEventType, list] = {}

        def register(self, et, handler):
            self.bindings.setdefault(et, []).append(handler)

    class _FakeFedSvc:
        def __init__(self) -> None:
            self._event_registry = _FakeRegistry()

        @property
        def own_instance_id(self) -> str:
            return "me"

    fed_svc = _FakeFedSvc()
    repo = _FakeFederationRepo()

    async def _noop(ev: FederationEvent) -> None:
        pass

    handler = SpaceRoutedHandler(
        federation_service=fed_svc,  # type: ignore[arg-type]
        federation_repo=repo,  # type: ignore[arg-type]
        event_dispatcher=_noop,
        target_eph_lookup=lambda _p: None,
    )
    handler.attach_to(fed_svc)  # type: ignore[arg-type]
    assert FederationEventType.SPACE_ROUTED in fed_svc._event_registry.bindings


async def test_two_hop_direct_unwrap_at_target():
    """The minimal mesh case: a → b with path=[a, b], position=0.
    b is the target (next_index == len(path)-1), so b unwraps
    immediately rather than forwarding."""
    nodes = _build_chain(["a", "b"])
    target_eph_pk = _mint_target_eph(nodes["b"])
    inner_payload = {"x": 1}
    await nodes["a"].handler.send_routed(
        path=["a", "b"],
        target_eph_pk_b64=target_eph_pk,
        inner_event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
        inner_payload=inner_payload,
    )
    for _ in range(4):
        await asyncio.sleep(0)
    assert len(nodes["b"].dispatched) == 1
    inner = nodes["b"].dispatched[0]
    assert inner.event_type is (FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK)
    assert inner.from_instance == "a"
    assert inner.routed_path == ["a", "b"]
    assert inner.payload == inner_payload
    # b did not forward; it only dispatched.
    assert all(
        s["event_type"] is not FederationEventType.SPACE_ROUTED
        for s in nodes["b"].fed.sent
    )


# ── Additional edge-case coverage ─────────────────────────────────────


def _make_envelope(
    *,
    route_id: str = "rid",
    path: list[str] | None = None,
    position: Any = 0,
    direction: str = "forward",
    inner_event_type: str = "space_invite_token_redeem",
    sealed: Any = None,
    from_instance: str = "a",
    to_instance: str = "b",
) -> FederationEvent:
    payload: dict[str, Any] = {
        "route_id": route_id,
        "path": list(path) if path is not None else ["a", "b"],
        "position": position,
        "direction": direction,
        "inner_event_type": inner_event_type,
        "sealed": sealed
        if sealed is not None
        else {"kem_suite": "x25519", "ciphertext": "x"},
    }
    return FederationEvent(
        msg_id="m-ec",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance=from_instance,
        to_instance=to_instance,
        timestamp="2026-05-22T00:00:00Z",
        payload=payload,
    )


async def test_send_routed_reply_raises_on_degenerate_reply_path():
    """A cached reply-state with a single-element path can never ship
    (no next hop) — :meth:`send_routed_reply` raises LookupError."""
    nodes = _build_chain(["a"])
    handler = nodes["a"].handler
    # Seed a degenerate reply-state with reply_path of length 1.
    priv, pub = routed_crypto.generate_ephemeral_keypair()
    _opriv, opub = routed_crypto.generate_ephemeral_keypair()
    handler._reply_eph_state["rid-degen"] = (
        priv,
        pub,
        opub,
        ["a"],
        float("inf"),
    )
    with pytest.raises(LookupError, match="reply_path too short"):
        await handler.send_routed_reply(
            route_id="rid-degen",
            inner_event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
            inner_payload={},
        )


async def test_missing_position_is_dropped():
    """Envelope lacking ``position`` entirely is dropped."""
    nodes = _build_chain(["a", "b"])
    handler_b = nodes["b"].handler
    ev = FederationEvent(
        msg_id="m-no-pos",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "r1",
            "path": ["a", "b"],
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []
    assert nodes["b"].fed.sent == []


async def test_non_int_position_is_dropped():
    """Envelope whose ``position`` is a non-numeric string is dropped."""
    nodes = _build_chain(["a", "b"])
    handler_b = nodes["b"].handler
    ev = _make_envelope(position="not-an-int", path=["a", "b"])
    await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []
    assert nodes["b"].fed.sent == []


async def test_position_out_of_range_is_dropped():
    """``position`` beyond the end of the path → drop."""
    nodes = _build_chain(["a", "b"])
    handler_b = nodes["b"].handler
    ev = _make_envelope(position=99, path=["a", "b"])
    await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []
    assert nodes["b"].fed.sent == []


async def test_negative_position_is_dropped():
    """Negative ``position`` → drop."""
    nodes = _build_chain(["a", "b"])
    handler_b = nodes["b"].handler
    ev = _make_envelope(position=-1, path=["a", "b"])
    await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []


async def test_from_instance_disagreeing_with_path_position_is_dropped(
    caplog,
):
    """The path[position] vs from_instance mismatch is an anti-spoof drop:
    the previous hop named in the path must be the authenticated sender, or
    the envelope is mis-routed / replayed off-path and must not be forwarded.
    """
    nodes = _build_chain(["a", "b", "c"])
    handler_b = nodes["b"].handler
    nodes["b"].fed.sent.clear()
    nodes["b"].dispatched.clear()
    # path[0] = "z" but from_instance = "a" → mismatch → drop.
    ev = FederationEvent(
        msg_id="m-mismatch",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "rid-mismatch",
            "path": ["z", "b", "c"],
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"kem_suite": "x25519", "ciphertext": "x"},
        },
    )
    with caplog.at_level(
        logging.WARNING, logger="socialhome.federation.routed_envelope"
    ):
        await handler_b._on_routed(ev)
    # Dropped: not forwarded, not dispatched, and the mismatch warned.
    assert nodes["b"].fed.sent == []
    assert nodes["b"].dispatched == []
    assert any(
        "from_instance=" in rec.message and "dropping" in rec.message
        for rec in caplog.records
    )


async def test_forward_send_event_failure_is_logged(caplog):
    """When the federation send_event raises on the forward hop, the
    handler swallows it with a warning rather than propagating."""
    nodes = _build_chain(["a", "b", "c"])
    handler_b = nodes["b"].handler

    async def _boom(**kwargs):
        raise RuntimeError("transport down")

    nodes["b"].fed.send_event = _boom  # type: ignore[method-assign]
    ev = _make_envelope(
        route_id="rid-fwd-fail",
        path=["a", "b", "c", "d"],  # we are b at idx 1; forward to c.
        position=0,
    )
    with caplog.at_level(
        logging.WARNING, logger="socialhome.federation.routed_envelope"
    ):
        await handler_b._on_routed(ev)
    assert any("forward to c failed" in r.message for r in caplog.records)


async def test_forward_unwrap_missing_target_eph_pk_drops(caplog):
    """Forward-leg unwrap where the sealed blob has no ``target_eph_pk``
    is dropped with a warning — we can't even look up the priv."""
    nodes = _build_chain(["a", "b"])
    handler_b = nodes["b"].handler
    ev = _make_envelope(
        route_id="rid-no-target-pub",
        path=["a", "b"],
        position=0,
        sealed={"kem_suite": "x25519", "ciphertext": "x"},  # no target_eph_pk
    )
    with caplog.at_level(
        logging.WARNING, logger="socialhome.federation.routed_envelope"
    ):
        await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []
    assert any("missing target_eph_pk" in r.message for r in caplog.records)


async def test_forward_unwrap_garbage_ciphertext_drops(caplog):
    """Forward-leg unwrap where AES-GCM decrypt fails on garbage
    ciphertext is dropped with a warning."""
    nodes = _build_chain(["a", "b"])
    pub = _mint_target_eph(nodes["b"])
    # Build a sealed blob that satisfies the wire shape but has a
    # tampered ciphertext that won't AES-decrypt.
    sealed = {
        "kem_suite": "x25519",
        "origin_eph_pk": pub,  # any valid b64-style pub works for KEM derive
        "target_eph_pk": pub,
        "nonce": "AAAAAAAAAAAAAAAA",
        "ciphertext": "AAAAAAAAAAAAAAAAAAAAAA",
    }
    ev = _make_envelope(
        route_id="rid-bad-ct",
        path=["a", "b"],
        position=0,
        sealed=sealed,
    )
    with caplog.at_level(
        logging.WARNING, logger="socialhome.federation.routed_envelope"
    ):
        await nodes["b"].handler._on_routed(ev)
    assert nodes["b"].dispatched == []
    assert any("forward unseal failed" in r.message for r in caplog.records)


async def test_forward_unwrap_non_json_drops(caplog):
    """Forward-leg unseal that returns non-JSON triggers the JSON-parse
    error branch."""
    nodes = _build_chain(["a", "b"])
    pub = _mint_target_eph(nodes["b"])
    sealed = {
        "kem_suite": "x25519",
        "origin_eph_pk": pub,
        "target_eph_pk": pub,
        "nonce": "AAAAAAAAAAAAAAAA",
        "ciphertext": "AAAAAAAAAAAAAAAAAAAAAA",
    }
    ev = _make_envelope(
        route_id="rid-non-json",
        path=["a", "b"],
        position=0,
        sealed=sealed,
    )
    with patch.object(routed_crypto, "unseal_inner_payload", return_value="not json"):
        with caplog.at_level(
            logging.WARNING, logger="socialhome.federation.routed_envelope"
        ):
            await nodes["b"].handler._on_routed(ev)
    assert nodes["b"].dispatched == []
    assert any("JSON parse failed" in r.message for r in caplog.records)


async def test_forward_unwrap_non_dict_json_drops(caplog):
    """Forward-leg unseal that returns valid JSON but not a dict (e.g.
    a list) is dropped."""
    nodes = _build_chain(["a", "b"])
    pub = _mint_target_eph(nodes["b"])
    sealed = {
        "kem_suite": "x25519",
        "origin_eph_pk": pub,
        "target_eph_pk": pub,
        "nonce": "AAAAAAAAAAAAAAAA",
        "ciphertext": "AAAAAAAAAAAAAAAAAAAAAA",
    }
    ev = _make_envelope(
        route_id="rid-non-dict",
        path=["a", "b"],
        position=0,
        sealed=sealed,
    )
    with patch.object(routed_crypto, "unseal_inner_payload", return_value="[]"):
        with caplog.at_level(
            logging.WARNING, logger="socialhome.federation.routed_envelope"
        ):
            await nodes["b"].handler._on_routed(ev)
    assert nodes["b"].dispatched == []
    assert any("decoded inner_payload is not a" in r.message for r in caplog.records)


async def test_reply_unwrap_without_origin_state_drops(caplog):
    """Reply-leg unwrap with no cached origin_eph_state for the
    route_id → drop + warn (expired or unsolicited reply)."""
    nodes = _build_chain(["a", "b"])
    handler_a = nodes["a"].handler
    # a is the origin (path[0]=a, path[-1]=b would be a normal forward;
    # for a reply leg the path is reversed: [b, a]). We are 'a' at
    # next_index=1.
    sealed = {
        "kem_suite": "x25519",
        "origin_eph_pk": "doesnt-matter",
        "target_eph_pk": "also-doesnt-matter",
        "nonce": "AAAAAAAAAAAAAAAA",
        "ciphertext": "AAAAAAAAAAAAAAAAAAAAAA",
    }
    ev = FederationEvent(
        msg_id="m-no-origin-state",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="b",
        to_instance="a",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "rid-unsolicited",
            "path": ["b", "a"],
            "position": 0,
            "direction": "reply",
            "inner_event_type": "space_invite_token_redeem_ack",
            "sealed": sealed,
        },
    )
    with caplog.at_level(
        logging.WARNING, logger="socialhome.federation.routed_envelope"
    ):
        await handler_a._on_routed(ev)
    assert nodes["a"].dispatched == []
    assert any("no cached origin_eph_priv" in r.message for r in caplog.records)


async def test_reply_unwrap_garbage_ciphertext_drops(caplog):
    """Reply-leg unseal that fails AES-GCM tag check → drop + warn."""
    nodes = _build_chain(["a", "b"])
    handler_a = nodes["a"].handler
    # Seed origin_eph_state so the cache lookup hits, but with a priv
    # that won't decrypt the bogus ciphertext.
    priv, pub = routed_crypto.generate_ephemeral_keypair()
    handler_a._origin_eph_state["rid-reply-bad"] = (priv, pub, float("inf"))
    sealed = {
        "kem_suite": "x25519",
        "origin_eph_pk": pub,
        "target_eph_pk": pub,
        "nonce": "AAAAAAAAAAAAAAAA",
        "ciphertext": "AAAAAAAAAAAAAAAAAAAAAA",
    }
    ev = FederationEvent(
        msg_id="m-reply-bad",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="b",
        to_instance="a",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "rid-reply-bad",
            "path": ["b", "a"],
            "position": 0,
            "direction": "reply",
            "inner_event_type": "space_invite_token_redeem_ack",
            "sealed": sealed,
        },
    )
    with caplog.at_level(
        logging.WARNING, logger="socialhome.federation.routed_envelope"
    ):
        await handler_a._on_routed(ev)
    assert nodes["a"].dispatched == []
    assert any("reply unseal failed" in r.message for r in caplog.records)


async def test_reply_unwrap_non_json_drops(caplog):
    """Reply-leg unseal that returns non-JSON → JSON-parse drop."""
    nodes = _build_chain(["a", "b"])
    handler_a = nodes["a"].handler
    priv, pub = routed_crypto.generate_ephemeral_keypair()
    handler_a._origin_eph_state["rid-reply-nojson"] = (priv, pub, float("inf"))
    sealed = {
        "kem_suite": "x25519",
        "origin_eph_pk": pub,
        "target_eph_pk": pub,
        "nonce": "AAAAAAAAAAAAAAAA",
        "ciphertext": "AAAAAAAAAAAAAAAAAAAAAA",
    }
    ev = FederationEvent(
        msg_id="m-reply-nojson",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="b",
        to_instance="a",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "rid-reply-nojson",
            "path": ["b", "a"],
            "position": 0,
            "direction": "reply",
            "inner_event_type": "space_invite_token_redeem_ack",
            "sealed": sealed,
        },
    )
    with patch.object(routed_crypto, "unseal_reply_payload", return_value="not json"):
        with caplog.at_level(
            logging.WARNING, logger="socialhome.federation.routed_envelope"
        ):
            await handler_a._on_routed(ev)
    assert nodes["a"].dispatched == []
    assert any(
        "reply inner_payload JSON parse failed" in r.message for r in caplog.records
    )


async def test_reply_unwrap_non_dict_json_drops(caplog):
    """Reply-leg unseal that returns valid JSON but a list → drop."""
    nodes = _build_chain(["a", "b"])
    handler_a = nodes["a"].handler
    priv, pub = routed_crypto.generate_ephemeral_keypair()
    handler_a._origin_eph_state["rid-reply-list"] = (priv, pub, float("inf"))
    sealed = {
        "kem_suite": "x25519",
        "origin_eph_pk": pub,
        "target_eph_pk": pub,
        "nonce": "AAAAAAAAAAAAAAAA",
        "ciphertext": "AAAAAAAAAAAAAAAAAAAAAA",
    }
    ev = FederationEvent(
        msg_id="m-reply-list",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="b",
        to_instance="a",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "rid-reply-list",
            "path": ["b", "a"],
            "position": 0,
            "direction": "reply",
            "inner_event_type": "space_invite_token_redeem_ack",
            "sealed": sealed,
        },
    )
    with patch.object(routed_crypto, "unseal_reply_payload", return_value="[]"):
        with caplog.at_level(
            logging.WARNING, logger="socialhome.federation.routed_envelope"
        ):
            await handler_a._on_routed(ev)
    assert nodes["a"].dispatched == []
    assert any("reply inner_payload is not a dict" in r.message for r in caplog.records)
