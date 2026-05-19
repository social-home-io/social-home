"""UsersSyncOutbound — push the full household roster to a new peer.

On :class:`PairingConfirmed` the service sends a single
``USERS_SYNC`` envelope carrying every (visible) local user, so the
peer's ``remote_users`` mirror is populated immediately instead of
trickling in one ``USER_UPDATED`` at a time as members edit their
profiles. The receiver-side handler already exists at
:meth:`FederationInboundService._on_users_sync`.
"""

from __future__ import annotations

import base64

import pytest

from socialhome.domain.events import PairingConfirmed
from socialhome.domain.federation import FederationEventType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.users_sync_outbound import UsersSyncOutbound


class _FakeFederationService:
    def __init__(self, own_instance_id: str = "own-inst") -> None:
        self._own_instance_id = own_instance_id
        self.sent: list[tuple[str, FederationEventType, dict]] = []

    async def send_event(self, *, to_instance_id, event_type, payload):
        self.sent.append((to_instance_id, event_type, payload))
        return None


class _FakeUser:
    def __init__(
        self,
        user_id: str,
        username: str,
        display_name: str,
        bio: str | None = None,
        picture_hash: str | None = None,
    ) -> None:
        self.user_id = user_id
        self.username = username
        self.display_name = display_name
        self.bio = bio
        self.picture_hash = picture_hash


class _FakeUserRepo:
    def __init__(self, users: list[_FakeUser]) -> None:
        self._users = users

    async def list_active(self):
        return list(self._users)


class _FakePictureRepo:
    """Returns ``(bytes, hash)`` for a known user_id or ``None``."""

    def __init__(self, pics: dict[str, bytes]) -> None:
        self._pics = pics

    async def get_user_picture(self, user_id: str):
        b = self._pics.get(user_id)
        if b is None:
            return None
        return b, f"hash-{user_id}"


class _FakeVisibilityRepo:
    def __init__(self) -> None:
        self._hidden: dict[str, set[str]] = {}

    def hide(self, peer: str, user_id: str) -> None:
        self._hidden.setdefault(peer, set()).add(user_id)

    async def hidden_user_ids_for_peer(self, peer: str) -> frozenset[str]:
        return frozenset(self._hidden.get(peer, set()))


@pytest.fixture
def env():
    bus = EventBus()
    fed = _FakeFederationService()
    user_repo = _FakeUserRepo(
        [
            _FakeUser("u-alice", "alice", "Alice", bio="hi", picture_hash=None),
            _FakeUser("u-maria", "maria", "Maria", bio=None, picture_hash="hm"),
        ]
    )
    out = UsersSyncOutbound(
        bus=bus,
        federation_service=fed,
        user_repo=user_repo,
    )
    out.wire()
    return bus, fed


async def test_fanout_on_pair_confirmed_sends_one_envelope(env):
    """One USERS_SYNC envelope, all local users in the payload."""
    bus, fed = env
    await bus.publish(PairingConfirmed(instance_id="peer-new"))
    assert len(fed.sent) == 1
    to, event_type, payload = fed.sent[0]
    assert to == "peer-new"
    assert event_type is FederationEventType.USERS_SYNC
    user_ids = [u["user_id"] for u in payload["users"]]
    assert sorted(user_ids) == ["u-alice", "u-maria"]
    # Each entry carries the public-shape fields the inbound handler
    # expects (mirrors USER_UPDATED).
    alice = next(u for u in payload["users"] if u["user_id"] == "u-alice")
    assert alice == {
        "user_id": "u-alice",
        "username": "alice",
        "display_name": "Alice",
        "bio": "hi",
        "picture_hash": None,
    }


async def test_skips_self_pair_confirmed(env):
    """``PairingConfirmed`` for our own instance id is a no-op — the
    own-instance hook fires on bootstrap of the local row and must
    not produce a self-addressed envelope."""
    bus, fed = env
    await bus.publish(PairingConfirmed(instance_id="own-inst"))
    assert fed.sent == []


async def test_empty_pair_confirmed_id_is_noop(env):
    bus, fed = env
    await bus.publish(PairingConfirmed(instance_id=""))
    assert fed.sent == []


async def test_no_local_users_no_send():
    """When there are no local users to advertise we don't send an
    empty envelope — keeps the wire quiet and avoids the receiver
    processing a zero-row sync."""
    bus = EventBus()
    fed = _FakeFederationService()
    user_repo = _FakeUserRepo([])
    out = UsersSyncOutbound(
        bus=bus,
        federation_service=fed,
        user_repo=user_repo,
    )
    out.wire()
    await bus.publish(PairingConfirmed(instance_id="peer-new"))
    assert fed.sent == []


async def test_visibility_repo_filters_per_peer():
    """Admin's per-peer hide list keeps a hidden user out of the
    snapshot. Mirrors the ProfileFederationOutbound semantics so the
    two outbound paths stay consistent."""
    bus = EventBus()
    fed = _FakeFederationService()
    user_repo = _FakeUserRepo(
        [
            _FakeUser("u-alice", "alice", "Alice"),
            _FakeUser("u-bob", "bob", "Bob"),
        ]
    )
    vis = _FakeVisibilityRepo()
    vis.hide("peer-new", "u-bob")
    out = UsersSyncOutbound(
        bus=bus,
        federation_service=fed,
        user_repo=user_repo,
        visibility_repo=vis,
    )
    out.wire()
    await bus.publish(PairingConfirmed(instance_id="peer-new"))
    payload = fed.sent[0][2]
    user_ids = [u["user_id"] for u in payload["users"]]
    assert user_ids == ["u-alice"]


async def test_visibility_repo_hides_everyone_then_no_send():
    """If every local user is hidden from the new peer, the envelope
    is suppressed entirely (same rule as the empty-roster case)."""
    bus = EventBus()
    fed = _FakeFederationService()
    user_repo = _FakeUserRepo(
        [
            _FakeUser("u-alice", "alice", "Alice"),
        ]
    )
    vis = _FakeVisibilityRepo()
    vis.hide("peer-new", "u-alice")
    out = UsersSyncOutbound(
        bus=bus,
        federation_service=fed,
        user_repo=user_repo,
        visibility_repo=vis,
    )
    out.wire()
    await bus.publish(PairingConfirmed(instance_id="peer-new"))
    assert fed.sent == []


async def test_picture_bytes_attached_when_available():
    """Users with a picture_hash AND bytes in the picture repo get a
    base64 ``picture_webp_base64`` field so the peer can render the
    avatar immediately on receive."""
    bus = EventBus()
    fed = _FakeFederationService()
    user_repo = _FakeUserRepo(
        [
            _FakeUser("u-alice", "alice", "Alice", picture_hash="h-alice"),
            _FakeUser("u-bob", "bob", "Bob", picture_hash="h-bob"),
            # No hash → no lookup, no bytes attached.
            _FakeUser("u-carol", "carol", "Carol", picture_hash=None),
        ]
    )
    pics = _FakePictureRepo({"u-alice": b"\x00\x01\x02"})
    out = UsersSyncOutbound(
        bus=bus,
        federation_service=fed,
        user_repo=user_repo,
        profile_picture_repo=pics,
    )
    out.wire()
    await bus.publish(PairingConfirmed(instance_id="peer-new"))
    payload = fed.sent[0][2]
    by_id = {u["user_id"]: u for u in payload["users"]}
    # Alice has both hash + bytes → base64 attached.
    assert by_id["u-alice"]["picture_webp_base64"] == base64.b64encode(
        b"\x00\x01\x02",
    ).decode("ascii")
    # Bob has a hash but no bytes stored → no base64 field, hash kept.
    assert "picture_webp_base64" not in by_id["u-bob"]
    assert by_id["u-bob"]["picture_hash"] == "h-bob"
    # Carol has no hash at all → no lookup attempted, no base64 field.
    assert "picture_webp_base64" not in by_id["u-carol"]
