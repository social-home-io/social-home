"""Tests for :class:`RouteDiscoveryService` (§D2 PR 2 / v_6 mesh).

Covers:

* Local short-circuit: ``target == self`` resolves synchronously with
  a freshly-minted target ephemeral.
* Direct-peer discovery: a CONFIRMED v_6 peer resolves via one probe,
  returning ``([self, peer], target_eph_pk)``.
* Indirect discovery: a small graph ``a—b—c`` finds ``([a, b, c],
  target_eph_pk)``.
* Loop prevention: a probe with a known ``request_id`` is dropped.
* Cycle prevention: a probe with self in ``hops_traversed`` is dropped.
* Hop budget: ``max_hops=2`` on a depth-3 target returns ``None``.
* Cache hit: second discovery for the same target within TTL skips
  the probe entirely.
* Random tie-break: when two equally-short paths land, both pick
  variants appear over N runs.
* Sub-v_6 peers are filtered out of relay forwarding + can't act as
  the target (no v_6 means no ephemeral mint).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    PairingStatus,
)
from socialhome.federation.route_discovery import (
    RouteDiscoveryService,
    _CachedRoute,
    _PendingDiscovery,
)


# ── Test doubles ──────────────────────────────────────────────────────


@dataclass
class _FakeInstance:
    """Stand-in for :class:`RemoteInstance`."""

    id: str
    status: PairingStatus = PairingStatus.CONFIRMED
    proto_version: int = 6


class _FakeFederationRepo:
    def __init__(self, instances: dict[str, _FakeInstance] | None = None) -> None:
        self._instances: dict[str, _FakeInstance] = instances or {}

    async def get_instance(self, instance_id: str) -> _FakeInstance | None:
        return self._instances.get(instance_id)

    async def list_instances(
        self,
        *,
        source: str | None = None,
        status: str | None = None,
    ) -> list[_FakeInstance]:
        out = list(self._instances.values())
        if status is not None:
            out = [i for i in out if i.status.value == status]
        return out


@dataclass
class _Node:
    """One instance in the simulated mesh.

    Holds the federation repo for the node, the wired-up route
    discovery service, and a dict of neighbour nodes (``id ->
    _Node``) that the test driver uses to physically deliver
    envelopes from one node to another.
    """

    instance_id: str
    repo: _FakeFederationRepo
    fed: "_LinkedFederationService"
    service: RouteDiscoveryService
    neighbours: dict[str, "_Node"] = field(default_factory=dict)


class _LinkedFederationService:
    """A test ``FederationService`` shim.

    Each node has one of these. ``send_event`` dispatches the envelope
    straight onto the destination node's :class:`RouteDiscoveryService`
    so the round-trip runs in-memory without any real transport.

    Tracks every outbound for assertions.
    """

    def __init__(self, *, own_instance_id: str, repo: _FakeFederationRepo) -> None:
        self._own_instance_id = own_instance_id
        self._repo = repo
        #: dest_instance_id → other node (set by the test driver).
        self.peers: dict[str, _Node] = {}
        self.sent: list[dict[str, Any]] = []
        # Optional bytewise hook so a test can force a send to fail.
        self.fail_to: set[str] = set()

    @property
    def own_instance_id(self) -> str:
        return self._own_instance_id

    async def peer_supports(self, instance_id: str, *, min_version: int) -> bool:
        peer = await self._repo.get_instance(instance_id)
        if peer is None:
            return False
        return peer.proto_version >= min_version

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
        if to_instance_id in self.fail_to:
            raise RuntimeError(f"forced send failure to {to_instance_id}")
        dest = self.peers.get(to_instance_id)
        if dest is None:
            return SimpleNamespace(ok=False, instance_id=to_instance_id)
        ev = FederationEvent(
            msg_id=f"m-{event_type.value}-{len(self.sent)}",
            event_type=event_type,
            from_instance=self._own_instance_id,
            to_instance=to_instance_id,
            timestamp="2026-05-22T00:00:00Z",
            payload=payload,
        )

        # Run the destination handler in a background task so the
        # caller's ``await`` returns immediately (mirrors the real
        # transport's fire-and-forget shape).
        async def _deliver() -> None:
            handler_map = {
                FederationEventType.SPACE_FIND_ROUTE: (dest.service._on_find_route),
                FederationEventType.SPACE_ROUTE_FOUND: (dest.service._on_route_found),
            }
            handler = handler_map.get(event_type)
            if handler is not None:
                await handler(ev)

        asyncio.create_task(_deliver())
        return SimpleNamespace(ok=True, instance_id=to_instance_id)


def _build_mesh(
    graph: dict[str, list[str]],
    *,
    proto_versions: dict[str, int] | None = None,
    discovery_timeout_s: float = 0.05,
    max_hops: int = 3,
) -> dict[str, _Node]:
    """Build a network of nodes following the adjacency map.

    ``graph[n]`` lists ``n``'s confirmed neighbours. Each neighbour
    relationship is bidirectional — listing ``a -> [b]`` in the map
    implies a sees b on its repo + b sees a on its repo.

    Each peer entry is rendered into both nodes' ``RemoteInstance``
    rows via ``_FakeFederationRepo``. ``proto_versions[node_id]``
    overrides the default ``proto_version=6`` for a specific node.
    """
    proto_versions = proto_versions or {}
    nodes: dict[str, _Node] = {}
    for node_id in graph:
        repo = _FakeFederationRepo()
        fed = _LinkedFederationService(own_instance_id=node_id, repo=repo)
        service = RouteDiscoveryService(
            federation_service=fed,  # type: ignore[arg-type]
            federation_repo=repo,  # type: ignore[arg-type]
            max_hops=max_hops,
            discovery_timeout_s=discovery_timeout_s,
            cache_ttl_s=60.0,
            seen_ttl_s=60.0,
        )
        nodes[node_id] = _Node(
            instance_id=node_id,
            repo=repo,
            fed=fed,
            service=service,
        )
    # Wire neighbour repos + peer maps.
    for node_id, neighbour_ids in graph.items():
        node = nodes[node_id]
        for nb_id in neighbour_ids:
            nb = nodes[nb_id]
            pv = proto_versions.get(nb_id, 6)
            node.repo._instances[nb_id] = _FakeInstance(
                id=nb_id,
                status=PairingStatus.CONFIRMED,
                proto_version=pv,
            )
            node.fed.peers[nb_id] = nb
            node.neighbours[nb_id] = nb
    return nodes


# ── Tests ─────────────────────────────────────────────────────────────


async def test_local_target_returns_self_path():
    """A request to discover the route to *our own* instance returns
    ``([self], target_eph_pk)`` without any probe — and the target
    ephemeral priv is cached on us for the inbound SPACE_ROUTED."""
    nodes = _build_mesh({"a": []})
    result = await nodes["a"].service.discover_route("a")
    assert result is not None
    path, target_eph_pk = result
    assert path == ["a"]
    assert target_eph_pk  # non-empty b64url
    # The corresponding priv is cached locally for unseal.
    assert nodes["a"].service.lookup_target_eph_priv(target_eph_pk) is not None
    # No FIND_ROUTE shipped.
    assert nodes["a"].fed.sent == []


async def test_direct_peer_resolves_via_single_hop_probe():
    """A confirmed direct v_6 peer resolves through a one-hop probe
    (no static short-circuit): the target answers with its freshly-
    minted ephemeral pub, so the origin can seal against it."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    result = await nodes["a"].service.discover_route("b")
    assert result is not None
    path, target_eph_pk = result
    assert path == ["a", "b"]
    assert target_eph_pk
    # The target ('b') minted the priv and cached it for unseal.
    assert nodes["b"].service.lookup_target_eph_priv(target_eph_pk) is not None


async def test_indirect_discovery_finds_three_hop_path():
    """In ``a — b — c`` (a paired with b, b paired with c, a NOT
    paired with c), discovering c from a yields ``([a, b, c],
    target_eph_pk)``."""
    nodes = _build_mesh({"a": ["b"], "b": ["a", "c"], "c": ["b"]})
    result = await nodes["a"].service.discover_route("c")
    assert result is not None
    path, target_eph_pk = result
    assert path == ["a", "b", "c"]
    assert target_eph_pk
    assert nodes["c"].service.lookup_target_eph_priv(target_eph_pk) is not None


async def test_loop_prevention_drops_duplicate_request_id():
    """A relay that has already seen a ``request_id`` drops the
    inbound FIND_ROUTE silently."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    svc_b = nodes["b"].service
    svc_b._seen_requests["rid-x"] = float("inf")
    fed_b = nodes["b"].fed
    fed_b.sent.clear()
    ev = FederationEvent(
        msg_id="m1",
        event_type=FederationEventType.SPACE_FIND_ROUTE,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "rid-x",
            "target_instance_id": "z",
            "hops_traversed": ["a"],
            "max_hops": 3,
            "origin_instance_id": "a",
        },
    )
    await svc_b._on_find_route(ev)
    assert fed_b.sent == []


async def test_cycle_prevention_drops_self_in_hops_traversed():
    """If ``hops_traversed`` already includes us, drop — the chain
    has looped back through our node."""
    nodes = _build_mesh({"b": []})
    svc_b = nodes["b"].service
    fed_b = nodes["b"].fed
    ev = FederationEvent(
        msg_id="m-cycle",
        event_type=FederationEventType.SPACE_FIND_ROUTE,
        from_instance="x",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "rid-cycle",
            "target_instance_id": "z",
            "hops_traversed": ["a", "b", "c"],  # self ("b") present
            "max_hops": 3,
            "origin_instance_id": "a",
        },
    )
    await svc_b._on_find_route(ev)
    assert fed_b.sent == []


async def test_max_hops_budget_returns_none_when_target_too_deep():
    """With ``max_hops=2`` and target at depth 3 (``a → b → c → d``),
    no relay forwards far enough to reach d — returns ``None``."""
    nodes = _build_mesh(
        {"a": ["b"], "b": ["a", "c"], "c": ["b", "d"], "d": ["c"]},
        discovery_timeout_s=0.05,
        max_hops=2,
    )
    result = await nodes["a"].service.discover_route("d")
    assert result is None


async def test_cache_hit_on_second_discover():
    """A second ``discover_route`` for the same target within the
    cache TTL returns the cached ``(path, target_eph_pk)`` without
    re-running the probe.
    """
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    first = await nodes["a"].service.discover_route("b")
    assert first is not None
    first_path, first_eph = first
    assert first_path == ["a", "b"]
    assert "b" in nodes["a"].service._route_cache
    # Force a third-party look — drop b from the repo + retry. If the
    # cache is honored, we still get the *same* tuple back.
    nodes["a"].repo._instances.pop("b", None)
    nodes["a"].fed.peers.pop("b", None)
    cached = await nodes["a"].service.discover_route("b")
    assert cached == (["a", "b"], first_eph)


async def test_invalidate_drops_cache_entry():
    """``invalidate`` removes a cached path; subsequent discover
    runs a fresh probe."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    await nodes["a"].service.discover_route("b")
    assert "b" in nodes["a"].service._route_cache
    await nodes["a"].service.invalidate("b")
    assert "b" not in nodes["a"].service._route_cache


async def test_random_tie_break_over_equal_paths():
    """Two equally-short paths a→b→z and a→c→z — over N runs both
    relays appear as the first hop."""
    nodes = _build_mesh(
        {
            "a": ["b", "c"],
            "b": ["a", "z"],
            "c": ["a", "z"],
            "z": ["b", "c"],
        },
        # The collection window starts on the first response; both
        # branches need to land before it fires for tie-breaking to
        # actually have anything to break. Use a generous window so
        # we're testing the secrets.choice tie-break, not the
        # asyncio scheduler's response latency.
        discovery_timeout_s=0.2,
    )
    seen_first_hops: set[str] = set()
    for _ in range(20):
        # Clear cache so each iteration runs a fresh probe.
        await nodes["a"].service.invalidate("z")
        result = await nodes["a"].service.discover_route("z")
        assert result is not None
        path, _eph = result
        # Path is [a, ?, z] — capture the middle hop.
        assert len(path) == 3
        assert path[0] == "a"
        assert path[-1] == "z"
        seen_first_hops.add(path[1])
        if len(seen_first_hops) == 2:
            break
    assert seen_first_hops == {"b", "c"}, (
        f"random tie-break didn't sample both hops in 20 runs: {seen_first_hops}"
    )


async def test_sub_v6_peer_is_not_proposed_as_next_hop():
    """A v_5 neighbour is filtered out of relay forwarding: in the
    graph ``a — b(v5) — c`` discovering ``c`` returns ``None`` —
    a cannot reach c because b is mesh-incapable.
    """
    nodes = _build_mesh(
        {"a": ["b"], "b": ["a", "c"], "c": ["b"]},
        proto_versions={"b": 5},
        discovery_timeout_s=0.05,
    )
    result = await nodes["a"].service.discover_route("c")
    assert result is None


async def test_sub_v6_direct_peer_target_returns_none():
    """A v_5 direct peer can't be reached via the BFS probe (we won't
    propose them as a relay), and there's no static short-circuit
    anymore — so discovery returns ``None``."""
    nodes = _build_mesh(
        {"a": ["b"], "b": ["a"]},
        proto_versions={"b": 5},
        discovery_timeout_s=0.05,
    )
    result = await nodes["a"].service.discover_route("b")
    assert result is None


async def test_no_confirmed_peers_returns_none_immediately():
    """An origin with zero confirmed peers can't probe — returns
    ``None`` without shipping anything."""
    nodes = _build_mesh({"a": []})
    result = await nodes["a"].service.discover_route("missing")
    assert result is None
    assert nodes["a"].fed.sent == []


async def test_relay_forwards_route_found_back_to_caller():
    """A relay (``b``) that previously cached the caller (``a``) for
    a ``request_id`` forwards the inbound ROUTE_FOUND back to a,
    propagating the target's ephemeral pub through opaque.
    """
    nodes = _build_mesh({"a": ["b"], "b": ["a", "c"], "c": ["b"]})
    svc_b = nodes["b"].service
    # Mark request as having traversed a→b (so b knows the caller).
    svc_b._caller_cache["rid-fwd"] = ("a", float("inf"))
    svc_b._seen_requests["rid-fwd"] = float("inf")
    nodes["b"].fed.sent.clear()
    # Now c → b ROUTE_FOUND with the discovered path + target eph.
    ev = FederationEvent(
        msg_id="m-rf",
        event_type=FederationEventType.SPACE_ROUTE_FOUND,
        from_instance="c",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "rid-fwd",
            "path": ["a", "b", "c"],
            "target_eph_pk": "fake-pub-b64",
        },
    )
    await svc_b._on_route_found(ev)
    # b should have forwarded to a.
    assert len(nodes["b"].fed.sent) == 1
    fwd = nodes["b"].fed.sent[0]
    assert fwd["to"] == "a"
    assert fwd["event_type"] == FederationEventType.SPACE_ROUTE_FOUND
    assert fwd["payload"]["path"] == ["a", "b", "c"]
    # Relay never strips / replaces the target ephemeral.
    assert fwd["payload"]["target_eph_pk"] == "fake-pub-b64"


async def test_route_found_without_target_eph_is_dropped():
    """A ROUTE_FOUND missing the ``target_eph_pk`` field is dropped —
    we can't seal against an empty pub, so a relay propagating it
    would just deliver an unusable path to the origin."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    svc_b = nodes["b"].service
    svc_b._caller_cache["rid-missing-eph"] = ("a", float("inf"))
    svc_b._seen_requests["rid-missing-eph"] = float("inf")
    nodes["b"].fed.sent.clear()
    ev = FederationEvent(
        msg_id="m-rf-bad",
        event_type=FederationEventType.SPACE_ROUTE_FOUND,
        from_instance="c",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={"request_id": "rid-missing-eph", "path": ["a", "b", "c"]},
    )
    await svc_b._on_route_found(ev)
    assert nodes["b"].fed.sent == []


async def test_orphan_route_found_is_silently_dropped():
    """A ROUTE_FOUND for an unknown ``request_id`` (no pending, no
    caller cache) is dropped without raising."""
    nodes = _build_mesh({"b": []})
    svc_b = nodes["b"].service
    ev = FederationEvent(
        msg_id="m-orphan",
        event_type=FederationEventType.SPACE_ROUTE_FOUND,
        from_instance="x",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "no-such",
            "path": ["x", "b"],
            "target_eph_pk": "fake-pub-b64",
        },
    )
    await svc_b._on_route_found(ev)  # no exception
    assert nodes["b"].fed.sent == []


async def test_find_route_with_missing_request_id_is_dropped():
    """Defensive: a FIND_ROUTE without ``request_id`` doesn't trip
    any forwarding logic."""
    nodes = _build_mesh({"b": []})
    svc_b = nodes["b"].service
    ev = FederationEvent(
        msg_id="m-bad",
        event_type=FederationEventType.SPACE_FIND_ROUTE,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={"target_instance_id": "z", "hops_traversed": ["a"]},
    )
    await svc_b._on_find_route(ev)
    assert nodes["b"].fed.sent == []


async def test_attach_to_wires_two_handlers():
    """`attach_to` registers FIND_ROUTE + ROUTE_FOUND handlers."""

    class _FakeRegistry:
        def __init__(self) -> None:
            self.bindings: dict[FederationEventType, list] = {}

        def register(self, et, handler):
            self.bindings.setdefault(et, []).append(handler)

    class _FakeFedSvc:
        def __init__(self) -> None:
            self._event_registry = _FakeRegistry()
            self._own_instance_id = "me"

        @property
        def own_instance_id(self) -> str:
            return self._own_instance_id

    fed_svc = _FakeFedSvc()
    repo = _FakeFederationRepo()
    svc = RouteDiscoveryService(
        federation_service=fed_svc,  # type: ignore[arg-type]
        federation_repo=repo,  # type: ignore[arg-type]
    )
    svc.attach_to(fed_svc)  # type: ignore[arg-type]
    assert FederationEventType.SPACE_FIND_ROUTE in fed_svc._event_registry.bindings
    assert FederationEventType.SPACE_ROUTE_FOUND in fed_svc._event_registry.bindings


@pytest.mark.asyncio(loop_scope="function")
async def test_max_hops_relay_drops_when_hops_traversed_at_limit():
    """A relay that receives a probe whose ``hops_traversed`` already
    has length ``max_hops`` doesn't forward (budget exhausted)."""
    nodes = _build_mesh({"b": []}, max_hops=2)
    svc_b = nodes["b"].service
    ev = FederationEvent(
        msg_id="m-budget",
        event_type=FederationEventType.SPACE_FIND_ROUTE,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "rid-budget",
            "target_instance_id": "z",
            "hops_traversed": ["a"],  # adding self brings us to 2 = max_hops
            "max_hops": 2,
            "origin_instance_id": "a",
        },
    )
    await svc_b._on_find_route(ev)
    # No forward to a peer named 'z' (we don't have one) AND no
    # forward to anyone else either — len(new_hops) == max_hops.
    assert nodes["b"].fed.sent == []


# ── Additional edge-case coverage ─────────────────────────────────────


async def test_discover_route_returns_none_when_every_send_fails(caplog):
    """If every confirmed peer's ``send_event`` raises during the
    initial fan-out, ``sent == 0`` and discover returns None with no
    pending state left behind."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    nodes["a"].fed.fail_to = {"b"}
    result = await nodes["a"].service.discover_route("z")
    assert result is None
    # No pending entry leaked (the cleanup branch fired).
    assert nodes["a"].service._pending == {}


async def test_discover_route_continues_when_one_of_many_sends_fails():
    """If one of several peers' send_event raises but at least one
    succeeds, the discovery proceeds (and times out → returns None
    because the failing peer's branch never produced a response)."""
    nodes = _build_mesh(
        {"a": ["b", "x"], "b": ["a"], "x": ["a"]},
        discovery_timeout_s=0.05,
    )
    # Force the ship to 'x' to fail. 'b' is reachable but isn't the
    # target — discovery still resolves to None for unknown 'z'.
    nodes["a"].fed.fail_to = {"x"}
    result = await nodes["a"].service.discover_route("z")
    assert result is None
    # send tried both peers; ≥1 succeeded.
    assert any(s["to"] == "b" for s in nodes["a"].fed.sent)


async def test_lookup_target_eph_priv_unknown_pub_returns_none():
    """Unknown pub → None."""
    nodes = _build_mesh({"a": []})
    assert nodes["a"].service.lookup_target_eph_priv("not-a-real-pub") is None


async def test_lookup_target_eph_priv_expired_entry_returns_none_and_reaps():
    """An expired entry is lazily reaped on lookup."""
    nodes = _build_mesh({"a": []})
    svc = nodes["a"].service
    svc._target_eph_state["expired-pub"] = (
        "priv-data",
        time.monotonic() - 1.0,  # already expired
    )
    assert svc.lookup_target_eph_priv("expired-pub") is None
    # Reaped from state.
    assert "expired-pub" not in svc._target_eph_state


async def test_find_route_with_missing_target_is_dropped():
    """A FIND_ROUTE without ``target_instance_id`` is dropped (the
    missing request_id / target log path)."""
    nodes = _build_mesh({"b": []})
    svc_b = nodes["b"].service
    ev = FederationEvent(
        msg_id="m-no-target",
        event_type=FederationEventType.SPACE_FIND_ROUTE,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "rid-no-target",
            "hops_traversed": ["a"],
            "max_hops": 3,
            "origin_instance_id": "a",
        },
    )
    await svc_b._on_find_route(ev)
    assert nodes["b"].fed.sent == []


async def test_find_route_with_malformed_max_hops_falls_back_to_local_cap():
    """A FIND_ROUTE whose ``max_hops`` is not an int gracefully falls
    back to the local cap (no crash)."""
    nodes = _build_mesh({"a": ["b"], "b": ["a", "c"], "c": ["b"]}, max_hops=3)
    svc_b = nodes["b"].service
    ev = FederationEvent(
        msg_id="m-bad-mh",
        event_type=FederationEventType.SPACE_FIND_ROUTE,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "rid-bad-mh",
            "target_instance_id": "c",
            "hops_traversed": ["a"],
            "max_hops": {"not": "an int"},  # triggers TypeError → fallback
            "origin_instance_id": "a",
        },
    )
    await svc_b._on_find_route(ev)
    # b forwards to c (its only mesh-capable peer other than the caller).
    forwards = [
        s
        for s in nodes["b"].fed.sent
        if s["event_type"] is FederationEventType.SPACE_FIND_ROUTE
    ]
    assert any(s["to"] == "c" for s in forwards)


async def test_find_route_forward_send_failure_is_logged(caplog):
    """Relay-side forwarding: when ``send_event`` raises on the
    forward FIND_ROUTE, we swallow + warn."""
    nodes = _build_mesh({"a": ["b"], "b": ["a", "c"], "c": ["b"]})
    svc_b = nodes["b"].service
    nodes["b"].fed.fail_to = {"c"}
    ev = FederationEvent(
        msg_id="m-fwd-fail",
        event_type=FederationEventType.SPACE_FIND_ROUTE,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "rid-fwd-fail",
            "target_instance_id": "z",  # unknown, so b must forward
            "hops_traversed": ["a"],
            "max_hops": 3,
            "origin_instance_id": "a",
        },
    )
    await svc_b._on_find_route(ev)
    # We attempted the send and the warning fired.
    assert any("forward FIND_ROUTE to c failed" in r.message for r in caplog.records)


async def test_on_route_found_resolved_pending_is_ignored():
    """A ROUTE_FOUND that arrives after the pending entry was already
    marked resolved is a no-op (the future may already be done)."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    svc_a = nodes["a"].service
    loop = asyncio.get_event_loop()
    fut: asyncio.Future[tuple[list[str], str] | None] = loop.create_future()
    pending = _PendingDiscovery(future=fut, target="b")
    pending.resolved = True  # already decided
    svc_a._pending["rid-late"] = pending
    ev = FederationEvent(
        msg_id="m-late",
        event_type=FederationEventType.SPACE_ROUTE_FOUND,
        from_instance="b",
        to_instance="a",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "rid-late",
            "path": ["a", "b"],
            "target_eph_pk": "pub-x",
        },
    )
    await svc_a._on_route_found(ev)
    # Pending still has zero responses appended (early-return fired).
    assert pending.responses == []
    # Future remains pending; cancel it so no warning at teardown.
    fut.cancel()


async def test_on_route_found_unknown_request_id_relay_branch_is_silent():
    """Relay branch: a ROUTE_FOUND for a request_id we never relayed
    is dropped (no pending, no caller_cache)."""
    nodes = _build_mesh({"b": []})
    svc_b = nodes["b"].service
    ev = FederationEvent(
        msg_id="m-relay-orphan",
        event_type=FederationEventType.SPACE_ROUTE_FOUND,
        from_instance="c",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "never-cached",
            "path": ["a", "b", "c"],
            "target_eph_pk": "pub-x",
        },
    )
    await svc_b._on_route_found(ev)
    assert nodes["b"].fed.sent == []


async def test_on_route_found_relay_send_failure_is_logged(caplog):
    """Relay forwarding ROUTE_FOUND back to the cached caller: if
    ``send_event`` raises, swallow + warn."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    svc_b = nodes["b"].service
    svc_b._caller_cache["rid-fail"] = ("a", float("inf"))
    nodes["b"].fed.fail_to = {"a"}
    ev = FederationEvent(
        msg_id="m-rf-fail",
        event_type=FederationEventType.SPACE_ROUTE_FOUND,
        from_instance="c",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "rid-fail",
            "path": ["a", "b", "c"],
            "target_eph_pk": "pub-x",
        },
    )
    await svc_b._on_route_found(ev)
    assert any("forward ROUTE_FOUND to a failed" in r.message for r in caplog.records)


async def test_send_route_found_failure_is_logged(caplog):
    """``_send_route_found`` swallows ``send_event`` failures with a
    warning rather than propagating — used on the local-target branch
    of FIND_ROUTE."""
    # Build a single-node mesh and call _send_route_found directly.
    nodes = _build_mesh({"a": []})
    svc_a = nodes["a"].service
    # Configure the fed shim to fail on any to_instance_id.
    nodes["a"].fed.fail_to = {"x"}
    await svc_a._send_route_found(
        "x",
        "rid-srf",
        ["x", "a"],
        target_eph_pk="pub-x",
    )
    assert any("ROUTE_FOUND to x failed" in r.message for r in caplog.records)


async def test_resolve_after_window_no_responses_resolves_to_none():
    """If the collection window fires with zero responses bearing a
    target_eph_pk, the future resolves to None (defence in depth)."""
    nodes = _build_mesh({"a": []}, discovery_timeout_s=0.01)
    svc_a = nodes["a"].service
    loop = asyncio.get_event_loop()
    fut: asyncio.Future[tuple[list[str], str] | None] = loop.create_future()
    pending = _PendingDiscovery(future=fut, target="t")
    # A single response without a target_eph_pk — gets filtered out.
    pending.responses.append((["a", "t"], ""))
    svc_a._pending["rid-empty"] = pending
    await svc_a._resolve_after_window("rid-empty")
    assert pending.resolved is True
    assert fut.done()
    assert fut.result() is None


async def test_resolve_after_window_pending_missing_is_noop():
    """If the pending entry was already cleaned up, the window
    resolver is a no-op (no AttributeError)."""
    nodes = _build_mesh({"a": []}, discovery_timeout_s=0.01)
    svc_a = nodes["a"].service
    # _resolve_after_window simply returns when pending is None.
    await svc_a._resolve_after_window("nonexistent-rid")  # no exception


async def test_resolve_after_window_skips_set_result_when_future_already_done():
    """If the future was resolved out-of-band, ``set_result`` is
    skipped (avoids the "future already done" assertion)."""
    nodes = _build_mesh({"a": []}, discovery_timeout_s=0.01)
    svc_a = nodes["a"].service
    loop = asyncio.get_event_loop()
    fut: asyncio.Future[tuple[list[str], str] | None] = loop.create_future()
    fut.set_result(None)  # already done
    pending = _PendingDiscovery(future=fut, target="t")
    pending.responses.append((["a", "b"], "pub-x"))
    svc_a._pending["rid-done"] = pending
    # Should not raise even though the future is already done.
    await svc_a._resolve_after_window("rid-done")
    assert pending.resolved is True


async def test_resolve_after_window_no_valid_skips_set_result_when_done():
    """``not valid`` branch with the future already done: ``set_result``
    is skipped (covers the ``if not pending.future.done():`` False arm
    inside the empty-valid branch)."""
    nodes = _build_mesh({"a": []}, discovery_timeout_s=0.01)
    svc_a = nodes["a"].service
    loop = asyncio.get_event_loop()
    fut: asyncio.Future[tuple[list[str], str] | None] = loop.create_future()
    fut.set_result(None)  # already done
    pending = _PendingDiscovery(future=fut, target="t")
    # No valid responses (every response missing target_eph_pk).
    pending.responses.append((["a", "t"], ""))
    svc_a._pending["rid-novalid-done"] = pending
    await svc_a._resolve_after_window("rid-novalid-done")
    assert pending.resolved is True
    # Future still holds its original result; no exception raised.
    assert fut.result() is None


async def test_prune_expired_drops_expired_entries():
    """``_prune_expired`` reaps all four caches: seen_requests,
    caller_cache, route_cache, and target_eph_state."""
    nodes = _build_mesh({"a": []})
    svc = nodes["a"].service
    now = time.monotonic()
    # Seed expired entries in every cache.
    svc._seen_requests["old-req"] = now - 1.0
    svc._seen_requests["fresh-req"] = now + 60.0
    svc._caller_cache["old-call"] = ("peer", now - 1.0)
    svc._caller_cache["fresh-call"] = ("peer", now + 60.0)
    svc._route_cache["old-tgt"] = _CachedRoute(
        path=["a"], target_eph_pk="x", expires_at=now - 1.0
    )
    svc._route_cache["fresh-tgt"] = _CachedRoute(
        path=["a"], target_eph_pk="y", expires_at=now + 60.0
    )
    svc._target_eph_state["old-pub"] = ("priv-1", now - 1.0)
    svc._target_eph_state["fresh-pub"] = ("priv-2", now + 60.0)
    svc._prune_expired(now)
    assert set(svc._seen_requests) == {"fresh-req"}
    assert set(svc._caller_cache) == {"fresh-call"}
    assert set(svc._route_cache) == {"fresh-tgt"}
    assert set(svc._target_eph_state) == {"fresh-pub"}


async def test_mesh_capable_peers_filters_unsupported(monkeypatch):
    """A confirmed peer whose ``peer_supports`` returns False is
    filtered out — the ``continue`` path."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    fed_a = nodes["a"].fed

    async def _not_supported(instance_id: str, *, min_version: int) -> bool:
        return False

    monkeypatch.setattr(fed_a, "peer_supports", _not_supported)
    peers = await nodes["a"].service._mesh_capable_peers(exclude=set())
    assert peers == []
