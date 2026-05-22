"""Tests for :class:`SpaceInviteTokenRedeemCoordinator` (§D2).

Covers both halves of the cross-instance round-trip:

* **Sender side** — ``request_redeem`` against an unpaired peer raises
  immediately; against a CONFIRMED peer ships REDEEM and parks an
  awaitable Future keyed on the nonce.
* **Issuer side** — ``_on_redeem`` consumes the token, seats the
  remote redeemer, and ships ACK; on a missing / expired / exhausted
  token (or storage error) ships DENY with a ``reason``.
* **Round-trip** — wire two coordinator instances against a shared
  in-memory federation bus so a REDEEM minted by one resolves the
  other's Future via the issuer's natural ACK.
* **Timeout** — when the issuer never responds the Future raises
  ``TimeoutError`` and the pending entry is cleaned up.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    PairingStatus,
)
from socialhome.domain.space import SpacePermissionError
from socialhome.federation.invite_token_redeem import (
    SpaceInviteTokenRedeemCoordinator,
)


# ── Test doubles ──────────────────────────────────────────────────────


@dataclass
class _FakeInstance:
    """Stand-in for :class:`RemoteInstance` carrying just the fields the
    coordinator inspects."""

    id: str
    status: PairingStatus = PairingStatus.CONFIRMED


class _FakeFederationRepo:
    def __init__(self, instances: dict[str, _FakeInstance] | None = None):
        self._instances = instances or {}

    async def get_instance(self, instance_id):
        return self._instances.get(instance_id)


class _FakeUserRepo:
    def __init__(self, users: dict[str, object] | None = None):
        self._users = users or {}

    async def get_by_user_id(self, user_id):
        return self._users.get(user_id)


class _FakeSpaceRepo:
    """In-memory ``space_repo`` slice.

    Holds an `invite_tokens` dict the test pre-seeds; ``consume_invite_token``
    decrements ``uses_remaining`` atomically and returns the row (or None on
    expired / exhausted). ``add_space_instance`` + ``is_banned`` round-trip
    via plain attributes so tests can assert on them.
    """

    def __init__(self):
        self.tokens: dict[str, dict] = {}
        self.space_instances: list[tuple[str, str]] = []
        self.bans: set[tuple[str, str]] = set()
        # Allow tests to force an exception out of consume_invite_token.
        self.consume_should_raise: Exception | None = None

    async def consume_invite_token(self, token):
        if self.consume_should_raise is not None:
            raise self.consume_should_raise
        row = self.tokens.get(token)
        if row is None:
            return None
        if row.get("expired"):
            return None
        if row["uses_remaining"] <= 0:
            return None
        row["uses_remaining"] -= 1
        return {
            "space_id": row["space_id"],
            "created_by": row["created_by"],
            "uses_remaining": row["uses_remaining"],
            "expires_at": row.get("expires_at"),
        }

    async def add_space_instance(self, space_id, instance_id):
        self.space_instances.append((space_id, instance_id))

    async def is_banned(self, space_id, user_id):
        return (space_id, user_id) in self.bans


class _FakeRemoteMemberRepo:
    def __init__(self):
        self.added: list[dict] = []
        self.removed: list[tuple[str, str, str]] = []

    async def add(self, **kwargs):
        self.added.append(kwargs)

    async def remove(self, space_id, instance_id, user_id):
        self.removed.append((space_id, instance_id, user_id))


class _FakeFederationService:
    """Captures outbound ``send_event`` calls for assertion."""

    def __init__(self):
        self._event_registry = _FakeRegistry()
        self.sent: list[dict] = []

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        self.sent.append(
            {
                "to": to_instance_id,
                "event_type": event_type,
                "payload": payload,
            }
        )

        # Returning a SimpleNamespace mimics DeliveryResult enough for
        # the coordinator's needs — it never inspects the return value.
        return SimpleNamespace(ok=True, instance_id=to_instance_id)


class _FakeRegistry:
    def __init__(self):
        self.bindings: dict = {}

    def register(self, event_type, handler):
        self.bindings.setdefault(event_type, []).append(handler)


class _LinkedFederationService(_FakeFederationService):
    """A ``send_event`` shim that, instead of stashing the envelope,
    delivers it straight into a paired coordinator's registry so the
    round-trip test can run without any real transport.
    """

    def __init__(self):
        super().__init__()
        self.peer: SpaceInviteTokenRedeemCoordinator | None = None
        self.from_instance: str = ""
        self.to_instance: str = ""

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        self.sent.append(
            {
                "to": to_instance_id,
                "event_type": event_type,
                "payload": payload,
            }
        )
        if self.peer is None:
            return SimpleNamespace(ok=False, instance_id=to_instance_id)
        ev = FederationEvent(
            msg_id="m-" + event_type.value,
            event_type=event_type,
            from_instance=self.from_instance,
            to_instance=to_instance_id,
            timestamp="2026-05-22T00:00:00Z",
            payload=payload,
        )
        # Dispatch on the peer's registry in a background task so the
        # caller's ``await`` returns immediately (mirrors the real
        # transport's fire-and-forget shape).
        peer_reg = self.peer._federation._event_registry  # type: ignore[attr-defined]
        handlers = peer_reg.bindings.get(event_type, [])
        for handler in handlers:
            await handler(ev)
        return SimpleNamespace(ok=True, instance_id=to_instance_id)


# ── Coordinator factory ───────────────────────────────────────────────


def _make_user(user_id: str = "u-local"):
    return SimpleNamespace(
        username="alice",
        display_name="Alice",
        public_key="pk-alice",
        user_id=user_id,
    )


def _make_coordinator(
    *,
    federation=None,
    space_repo=None,
    remote_members=None,
    user_repo=None,
    federation_repo=None,
    timeout: float = 10.0,
):
    bus = AsyncMock()
    bus.publish = AsyncMock()
    return SpaceInviteTokenRedeemCoordinator(
        bus=bus,
        federation_service=federation or _FakeFederationService(),
        space_repo=space_repo or _FakeSpaceRepo(),
        space_remote_member_repo=remote_members or _FakeRemoteMemberRepo(),
        user_repo=user_repo or _FakeUserRepo({"u-local": _make_user()}),
        federation_repo=federation_repo or _FakeFederationRepo(),
        timeout=timeout,
    )


# ── Sender-side tests ─────────────────────────────────────────────────


async def test_redeem_against_unpaired_issuer_raises():
    """An issuer with no CONFIRMED ``RemoteInstance`` row rejects
    immediately — the SPA should pair first."""
    coord = _make_coordinator(federation_repo=_FakeFederationRepo({}))
    with pytest.raises(SpacePermissionError) as exc:
        await coord.request_redeem(
            "tkn",
            viewer_user_id="u-local",
            issuer_instance_id="peer-1",
        )
    assert "not a confirmed peer" in str(exc.value)


async def test_redeem_against_pending_issuer_raises():
    """A peer in PENDING_SENT — i.e. handshake half-done — is not yet
    eligible to receive REDEEM events."""
    fr = _FakeFederationRepo(
        {"peer-1": _FakeInstance("peer-1", status=PairingStatus.PENDING_SENT)},
    )
    coord = _make_coordinator(federation_repo=fr)
    with pytest.raises(SpacePermissionError):
        await coord.request_redeem(
            "tkn",
            viewer_user_id="u-local",
            issuer_instance_id="peer-1",
        )


async def test_redeem_missing_local_user_raises():
    """If the redeemer's local user row vanished between auth and
    redeem, surface a permission-shape error rather than a 500."""
    fr = _FakeFederationRepo({"peer-1": _FakeInstance("peer-1")})
    coord = _make_coordinator(federation_repo=fr, user_repo=_FakeUserRepo({}))
    with pytest.raises(SpacePermissionError):
        await coord.request_redeem(
            "tkn",
            viewer_user_id="u-local",
            issuer_instance_id="peer-1",
        )


# ── Round-trip tests ──────────────────────────────────────────────────


def _wire_pair(token_row: dict, *, redeemer_banned: bool = False):
    """Create a sender + issuer coordinator pair linked through
    ``_LinkedFederationService`` so the round-trip runs in-memory.

    Returns ``(sender, issuer, sender_fed, issuer_fed)``.
    """
    sender_fed = _LinkedFederationService()
    issuer_fed = _LinkedFederationService()
    issuer_repo = _FakeSpaceRepo()
    issuer_repo.tokens["good-token"] = token_row
    if redeemer_banned:
        issuer_repo.bans.add((token_row["space_id"], "u-local"))
    issuer_members = _FakeRemoteMemberRepo()

    sender = _make_coordinator(
        federation=sender_fed,
        federation_repo=_FakeFederationRepo(
            {"issuer-1": _FakeInstance("issuer-1")},
        ),
    )
    issuer = _make_coordinator(
        federation=issuer_fed,
        space_repo=issuer_repo,
        remote_members=issuer_members,
    )
    sender.attach_to(sender_fed)
    issuer.attach_to(issuer_fed)
    sender_fed.peer = issuer
    sender_fed.from_instance = "sender-1"
    sender_fed.to_instance = "issuer-1"
    issuer_fed.peer = sender
    issuer_fed.from_instance = "issuer-1"
    issuer_fed.to_instance = "sender-1"
    return (sender, issuer, sender_fed, issuer_fed, issuer_repo, issuer_members)


async def test_redeem_round_trip_success():
    """A live token + CONFIRMED peer → ACK arrives, sender's Future
    resolves with ``{space_id, role}``. Issuer's side seats the
    redeemer as a SpaceRemoteMember and adds the receiver's
    space_instance row."""
    sender, _issuer, _sf, _if, issuer_repo, issuer_members = _wire_pair(
        {
            "space_id": "sp-1",
            "created_by": "owner",
            "uses_remaining": 1,
        },
    )
    result = await sender.request_redeem(
        "good-token",
        viewer_user_id="u-local",
        issuer_instance_id="issuer-1",
    )
    assert result == {"space_id": "sp-1", "role": "member"}
    # Issuer seated the redeemer with the identity we shipped.
    assert len(issuer_members.added) == 1
    seat = issuer_members.added[0]
    assert seat["space_id"] == "sp-1"
    assert seat["instance_id"] == "sender-1"
    assert seat["user_id"] == "u-local"
    assert seat["user_pk"] == "pk-alice"
    assert seat["display_name"] == "Alice"
    # Issuer also registered the receiver as a space_instance + token
    # is now exhausted.
    assert ("sp-1", "sender-1") in issuer_repo.space_instances
    assert issuer_repo.tokens["good-token"]["uses_remaining"] == 0


async def test_redeem_round_trip_persists_local_space_instance():
    """On ACK the receiver adds its own ``space_instances`` row so the
    issuer's household will receive subsequent fan-outs."""
    sender, _issuer, *_rest = _wire_pair(
        {
            "space_id": "sp-2",
            "created_by": "owner",
            "uses_remaining": 1,
        },
    )
    # The sender's space_repo (separate from the issuer's) should
    # learn the (space_id, issuer_instance_id) mapping after ACK.
    sender_repo = sender._spaces  # type: ignore[attr-defined]
    await sender.request_redeem(
        "good-token",
        viewer_user_id="u-local",
        issuer_instance_id="issuer-1",
    )
    assert ("sp-2", "issuer-1") in sender_repo.space_instances


async def test_redeem_with_expired_token_sends_deny():
    """An issuer-side ``consume_invite_token`` miss → DENY with a
    user-visible reason → receiver's Future raises
    ``SpacePermissionError`` carrying that reason."""
    sender, _issuer, _sf, _if, issuer_repo, issuer_members = _wire_pair(
        {
            "space_id": "sp-3",
            "created_by": "owner",
            "uses_remaining": 1,
            "expired": True,
        },
    )
    with pytest.raises(SpacePermissionError) as exc:
        await sender.request_redeem(
            "good-token",
            viewer_user_id="u-local",
            issuer_instance_id="issuer-1",
        )
    assert "invalid, expired, or exhausted" in str(exc.value)
    # No member should have been seated.
    assert issuer_members.added == []


async def test_redeem_with_no_remaining_uses_sends_deny():
    """A token already burned through its uses_remaining → DENY,
    receiver raises with the same reason."""
    sender, _issuer, _sf, _if, _repo, issuer_members = _wire_pair(
        {
            "space_id": "sp-4",
            "created_by": "owner",
            "uses_remaining": 0,
        },
    )
    with pytest.raises(SpacePermissionError):
        await sender.request_redeem(
            "good-token",
            viewer_user_id="u-local",
            issuer_instance_id="issuer-1",
        )
    assert issuer_members.added == []


async def test_redeem_with_banned_redeemer_sends_deny():
    """A live token can still be vetoed by an issuer-side ban."""
    sender, _issuer, _sf, _if, _repo, issuer_members = _wire_pair(
        {
            "space_id": "sp-5",
            "created_by": "owner",
            "uses_remaining": 1,
        },
        redeemer_banned=True,
    )
    with pytest.raises(SpacePermissionError) as exc:
        await sender.request_redeem(
            "good-token",
            viewer_user_id="u-local",
            issuer_instance_id="issuer-1",
        )
    assert "banned" in str(exc.value).lower()
    assert issuer_members.added == []


async def test_redeem_with_unknown_token_sends_deny():
    """A token the issuer's repo doesn't recognise → DENY."""
    sender, _issuer, _sf, _if, _repo, _members = _wire_pair(
        # _wire_pair seeds "good-token"; we'll redeem a *different*
        # token below so the consume returns None.
        {
            "space_id": "sp-x",
            "created_by": "owner",
            "uses_remaining": 1,
        },
    )
    with pytest.raises(SpacePermissionError):
        await sender.request_redeem(
            "unknown-token",
            viewer_user_id="u-local",
            issuer_instance_id="issuer-1",
        )


async def test_redeem_issuer_storage_error_sends_deny():
    """If ``consume_invite_token`` raises on the issuer side, send DENY
    rather than leaving the receiver hanging."""
    sender, _issuer, _sf, _if, issuer_repo, _members = _wire_pair(
        {
            "space_id": "sp-6",
            "created_by": "owner",
            "uses_remaining": 1,
        },
    )
    issuer_repo.consume_should_raise = RuntimeError("disk full")
    with pytest.raises(SpacePermissionError) as exc:
        await sender.request_redeem(
            "good-token",
            viewer_user_id="u-local",
            issuer_instance_id="issuer-1",
        )
    assert "storage error" in str(exc.value)


# ── Timeout tests ─────────────────────────────────────────────────────


async def test_redeem_timeout_raises_timeout_error():
    """When the issuer never sends ACK / DENY, the Future resolves with
    ``TimeoutError`` and the pending dict is cleaned up so a late ACK
    won't trip an assertion."""
    fed = _FakeFederationService()  # absorbs send_event; never ACKs back
    coord = _make_coordinator(
        federation=fed,
        federation_repo=_FakeFederationRepo(
            {"issuer-1": _FakeInstance("issuer-1")},
        ),
        timeout=0.05,
    )
    with pytest.raises(TimeoutError):
        await coord.request_redeem(
            "any-token",
            viewer_user_id="u-local",
            issuer_instance_id="issuer-1",
        )
    # Pending registry is drained — a late ACK is a no-op.
    assert coord._pending == {}


# ── Attach / wiring tests ─────────────────────────────────────────────


async def test_attach_to_registers_three_handlers():
    """`attach_to` wires the three event-type → handler bindings."""
    coord = _make_coordinator()
    reg = _FakeRegistry()

    class _FakeFedSvc:
        def __init__(self):
            self._event_registry = reg

    coord.attach_to(_FakeFedSvc())  # type: ignore[arg-type]
    assert FederationEventType.SPACE_INVITE_TOKEN_REDEEM in reg.bindings
    assert FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK in reg.bindings
    assert FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY in reg.bindings


async def test_late_ack_after_timeout_is_noop():
    """A delayed ACK for a nonce that already timed out must not raise
    — the receiver may simply have moved on."""
    coord = _make_coordinator()
    ev = FederationEvent(
        msg_id="m1",
        event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
        from_instance="issuer-1",
        to_instance="sender-1",
        timestamp="2026-05-22T00:00:00Z",
        payload={"redeem_nonce": "stale", "space_id": "sp-x"},
    )
    await coord._on_redeem_ack(ev)  # no exception


async def test_redeem_missing_nonce_in_payload_skipped():
    """A REDEEM with no nonce — likely a malformed peer — is dropped."""
    fed = _FakeFederationService()
    coord = _make_coordinator(federation=fed)
    ev = FederationEvent(
        msg_id="m1",
        event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
        from_instance="peer-1",
        to_instance="me",
        timestamp="2026-05-22T00:00:00Z",
        payload={"invite_token": "tkn"},  # no redeem_nonce
    )
    await coord._on_redeem(ev)
    assert fed.sent == []  # no DENY shipped — we can't key it


async def test_redeem_missing_redeemer_user_id_sends_deny():
    """A REDEEM with a nonce but no redeemer_user_id → DENY (we have
    enough info to reply, but not enough to seat anyone)."""
    fed = _FakeFederationService()
    coord = _make_coordinator(federation=fed)
    ev = FederationEvent(
        msg_id="m1",
        event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
        from_instance="peer-1",
        to_instance="me",
        timestamp="2026-05-22T00:00:00Z",
        payload={"redeem_nonce": "n1", "invite_token": "tkn"},
    )
    await coord._on_redeem(ev)
    assert len(fed.sent) == 1
    assert (
        fed.sent[0]["event_type"] is FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY
    )


async def test_redeem_deny_with_no_reason_uses_default():
    """A DENY shipped without a ``reason`` still resolves the Future
    with a non-empty SpacePermissionError."""
    coord = _make_coordinator()
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    coord._pending["n1"] = fut
    ev = FederationEvent(
        msg_id="m1",
        event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY,
        from_instance="issuer-1",
        to_instance="me",
        timestamp="2026-05-22T00:00:00Z",
        payload={"redeem_nonce": "n1"},
    )
    await coord._on_redeem_deny(ev)
    with pytest.raises(SpacePermissionError) as exc:
        fut.result()
    assert str(exc.value)  # non-empty
