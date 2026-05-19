"""ProfileFederationOutbound — fan UserProfileUpdated to paired peers.

Mirrors the shape of ``test_sticky_federation_outbound.py``: an
in-memory ``EventBus``, a fake :class:`FederationService` recording
sends, and a fake federation repo returning the list of confirmed
peers. The peer-user visibility repo is optional — the suite covers
both the default (every peer sees every user) and the explicit-filter
shape.
"""

from __future__ import annotations

import pytest

from socialhome.domain.events import UserProfileUpdated
from socialhome.domain.federation import FederationEventType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.profile_federation_outbound import (
    ProfileFederationOutbound,
)


class _FakeFederationService:
    def __init__(self, own_instance_id: str = "own-inst") -> None:
        self._own_instance_id = own_instance_id
        self.sent: list[tuple[str, FederationEventType, dict]] = []

    async def send_event(self, *, to_instance_id, event_type, payload):
        self.sent.append((to_instance_id, event_type, payload))
        return None


class _Peer:
    def __init__(self, instance_id: str) -> None:
        self.id = instance_id


class _FakeFedRepo:
    def __init__(self, peers: list[str]) -> None:
        self._peers = peers

    async def list_instances(self, status: str):
        assert status == "confirmed"
        return [_Peer(p) for p in self._peers]


class _FakeVisibilityRepo:
    """Per-peer hide list. ``hidden_user_ids_for_peer`` returns the set
    of user_ids explicitly hidden from a given peer via :meth:`hide`."""

    def __init__(self) -> None:
        self._hidden: dict[str, set[str]] = {}

    def hide(self, peer: str, user_id: str) -> None:
        self._hidden.setdefault(peer, set()).add(user_id)

    async def hidden_user_ids_for_peer(self, peer: str) -> frozenset[str]:
        return frozenset(self._hidden.get(peer, set()))


def _event(**over) -> UserProfileUpdated:
    base = dict(
        user_id="u1",
        username="alice",
        display_name="Alice",
        bio="hello",
        picture_hash="h1",
        picture_webp=None,
    )
    base.update(over)
    return UserProfileUpdated(**base)


@pytest.fixture
def env():
    bus = EventBus()
    fed = _FakeFederationService()
    repo = _FakeFedRepo(["peer-1", "peer-2", "own-inst"])
    out = ProfileFederationOutbound(
        bus=bus,
        federation_service=fed,
        federation_repo=repo,
    )
    out.wire()
    return bus, fed


async def test_fanouts_to_every_paired_peer_excluding_self(env):
    bus, fed = env
    await bus.publish(_event())
    recipients = [r[0] for r in fed.sent]
    assert recipients == ["peer-1", "peer-2"]
    # All carry USER_UPDATED with the public payload shape.
    for to, event_type, payload in fed.sent:
        assert event_type is FederationEventType.USER_UPDATED
        assert payload == {
            "user_id": "u1",
            "username": "alice",
            "display_name": "Alice",
            "bio": "hello",
            "picture_hash": "h1",
        }


async def test_picture_bytes_base64d_when_present(env):
    bus, fed = env
    await bus.publish(_event(picture_webp=b"\x00\x01\x02"))
    for _to, _ev, payload in fed.sent:
        # base64-encoded ``\x00\x01\x02`` is ``AAEC``.
        assert payload["picture_webp_base64"] == "AAEC"


async def test_visibility_repo_hides_user_from_specific_peer():
    bus = EventBus()
    fed = _FakeFederationService()
    repo = _FakeFedRepo(["peer-1", "peer-2"])
    vis = _FakeVisibilityRepo()
    vis.hide("peer-2", "u1")  # u1 is hidden from peer-2 only
    out = ProfileFederationOutbound(
        bus=bus,
        federation_service=fed,
        federation_repo=repo,
        visibility_repo=vis,
    )
    out.wire()
    await bus.publish(_event())
    recipients = [r[0] for r in fed.sent]
    assert recipients == ["peer-1"]


async def test_no_peers_no_sends():
    bus = EventBus()
    fed = _FakeFederationService()
    out = ProfileFederationOutbound(
        bus=bus,
        federation_service=fed,
        federation_repo=_FakeFedRepo([]),
    )
    out.wire()
    await bus.publish(_event())
    assert fed.sent == []


async def test_peer_id_missing_is_skipped():
    """A peer row without an ``id`` (defensive — shouldn't happen in
    prod but the loop defends) is silently skipped."""
    bus = EventBus()
    fed = _FakeFederationService()

    class _BrokenPeerRepo:
        async def list_instances(self, status):
            class _NoId:
                pass

            return [_NoId(), _Peer("good")]

    out = ProfileFederationOutbound(
        bus=bus,
        federation_service=fed,
        federation_repo=_BrokenPeerRepo(),
    )
    out.wire()
    await bus.publish(_event())
    assert [r[0] for r in fed.sent] == ["good"]
