"""Coverage fill for :class:`SpaceService` federation-facing methods.

Covers remote invites (accept/decline), remote member removal, join
requests (approve/deny local + remote), and ``request_join_remote``.
Each test uses a MagicMock for FederationService so we never require
a real peer connection.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import (
    DeliveryResult,
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.space import (
    JoinMode,
    SpacePermissionError,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_service import SpaceService
from socialhome.services.user_service import UserService


@pytest.fixture
async def stack(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    space_repo = SqliteSpaceRepo(db)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    svc = SpaceService(
        space_repo,
        SqliteSpacePostRepo(db),
        user_repo,
        bus,
        own_instance_id=iid,
    )
    fed_svc = MagicMock()
    fed_svc.send_event = AsyncMock()
    # The space-service private-invite family delegates to
    # ``FederationService.send_with_mesh_fallback`` — default to a
    # successful direct-delivery result; tests override per-case.
    fed_svc.send_with_mesh_fallback = AsyncMock(
        return_value=DeliveryResult(instance_id="peer", ok=True),
    )
    fed_repo = MagicMock()
    fed_repo.get_instance = AsyncMock(
        return_value=RemoteInstance(
            id="peer",
            display_name="Peer",
            remote_identity_pk="ab" * 32,
            key_self_to_remote="k",
            key_remote_to_self="k",
            remote_inbox_url="https://peer",
            local_inbox_id="l",
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
        ),
    )
    svc.attach_federation(
        federation_service=fed_svc,
        federation_repo=fed_repo,
        remote_member_repo=SqliteSpaceRemoteMemberRepo(db),
    )

    class S:
        pass

    s = S()
    s.db = db
    s.svc = svc
    s.fed_svc = fed_svc
    s.fed_repo = fed_repo
    s.space_repo = space_repo
    s.user_svc = user_svc
    yield s
    await db.shutdown()


async def _user(stack, username):
    return await stack.user_svc.provision(
        username=username,
        display_name=username,
    )


# ── invite_remote_user ──────────────────────────────────────────────


async def test_invite_remote_user_rejects_unpaired_host(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="Private",
        space_type=SpaceType.PRIVATE,
    )
    # Federation surfaces no path → invite raises permission error.
    stack.fed_svc.send_with_mesh_fallback.return_value = DeliveryResult(
        instance_id="peer",
        ok=False,
        error="not_confirmed",
    )
    with pytest.raises(SpacePermissionError):
        await stack.svc.invite_remote_user(
            space.id,
            actor_username="alicehost",
            invitee_instance_id="peer",
            invitee_user_id="bob",
        )


async def test_invite_remote_user_happy(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="Private",
        space_type=SpaceType.PRIVATE,
    )
    token = await stack.svc.invite_remote_user(
        space.id,
        actor_username="alicehost",
        invitee_instance_id="peer",
        invitee_user_id="bob",
    )
    assert token
    stack.fed_svc.send_with_mesh_fallback.assert_awaited_once()
    # The outbound payload must carry ``invitee_user_id`` so
    # :meth:`PrivateSpaceInviteHandler._on_invite` doesn't early-
    # return on the recipient. Regression guard for
    # ``GET /api/remote_invites`` returning empty.
    call = stack.fed_svc.send_with_mesh_fallback.call_args
    payload = call.kwargs["payload"]
    assert payload["invitee_user_id"] == "bob"
    assert payload["invite_token"] == token


async def test_invite_remote_user_requires_federation():
    """Direct SpaceService without attach_federation must raise."""
    svc = SpaceService.__new__(SpaceService)
    svc._federation = None
    svc._federation_repo = None
    with pytest.raises(RuntimeError):
        await svc.invite_remote_user(
            "sp",
            actor_username="alicehost",
            invitee_instance_id="peer",
            invitee_user_id="bob",
        )


# ── accept/decline_remote_invite ───────────────────────────────────


async def test_accept_remote_invite_unknown_token_raises(stack):
    with pytest.raises(KeyError):
        await stack.svc.accept_remote_invite(
            token="bogus",
            user_id="u",
        )


async def test_accept_remote_invite_not_cross_household_raises(stack):
    """A remote-invitation row saved with no remote_instance_id yields ValueError."""
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
    )
    # Directly insert a row without remote_instance_id.
    await stack.db.enqueue(
        """INSERT INTO space_invitations(
               id, space_id, invited_user_id, invited_by, remote_instance_id,
               remote_user_id, invite_token, status, expires_at
           ) VALUES(?, ?, 'u', 'x', '', 'u', 'local-tkn', 'pending',
                    datetime('now', '+1 day'))""",
        ("inv-1", space.id),
    )
    with pytest.raises(ValueError):
        await stack.svc.accept_remote_invite(
            token="local-tkn",
            user_id="u",
        )


async def test_accept_remote_invite_happy(stack):
    bob = await _user(stack, "bobhost")
    # Pre-existing space on the OTHER household — seed a remote-invite
    # row pointing at bob.
    await stack.space_repo.save_remote_invitation(
        space_id="sp-on-the-other-side",
        invited_by="alicehost-id",
        remote_instance_id="peer",
        remote_user_id=bob.user_id,
        invite_token="tok-xyz",
        space_display_hint="S",
    )
    # Seed the local stub so the membership row insert succeeds.
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )

    stub = Space(
        id="sp-on-the-other-side",
        name="Remote space",
        owner_instance_id="peer",
        owner_username="alicehost",
        identity_public_key="",
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
        emoji="🏠",
        description="",
    )
    await stack.space_repo.save(stub)

    await stack.svc.accept_remote_invite(
        token="tok-xyz",
        user_id=bob.user_id,
    )

    stack.fed_svc.send_with_mesh_fallback.assert_awaited()
    # Regression guard for "host sees raw user_id instead of display
    # name" — the accept envelope MUST carry the invitee's display name
    # so the host's roster renders the human-readable label rather than
    # the bare ``uid-...`` hash. Earlier, the code looked up
    # ``users_repo.get_by_id`` which doesn't exist on the protocol;
    # ``hasattr`` returned False every time and ``invitee_display_name``
    # was always ``None``.
    call = stack.fed_svc.send_with_mesh_fallback.call_args
    payload = call.kwargs["payload"]
    assert payload["invitee_display_name"] == bob.display_name
    assert payload["invitee_user_id"] == bob.user_id


async def test_decline_remote_invite_unknown_token(stack):
    with pytest.raises(KeyError):
        await stack.svc.decline_remote_invite(
            token="nope",
            user_id="u",
        )


async def test_decline_remote_invite_not_cross_household(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
    )
    await stack.db.enqueue(
        """INSERT INTO space_invitations(
               id, space_id, invited_user_id, invited_by, remote_instance_id,
               remote_user_id, invite_token, status, expires_at
           ) VALUES(?, ?, 'u', 'x', '', 'u', 'loc-dec', 'pending',
                    datetime('now', '+1 day'))""",
        ("inv-dec", space.id),
    )
    with pytest.raises(ValueError):
        await stack.svc.decline_remote_invite(
            token="loc-dec",
            user_id="u",
        )


async def test_decline_remote_invite_happy(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
    )
    await stack.space_repo.save_remote_invitation(
        space_id=space.id,
        invited_by="alicehost-id",
        remote_instance_id="peer",
        remote_user_id="bob",
        invite_token="tok-decline",
        space_display_hint="S",
    )
    await stack.svc.decline_remote_invite(
        token="tok-decline",
        user_id="bob",
    )
    stack.fed_svc.send_with_mesh_fallback.assert_awaited()


# ── remove_remote_member ───────────────────────────────────────────


async def test_remove_remote_member_happy(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
    )
    await stack.svc.remove_remote_member(
        space.id,
        actor_username="alicehost",
        instance_id="peer",
        user_id="bob",
    )
    stack.fed_svc.send_with_mesh_fallback.assert_awaited()


# ── request_join_remote ────────────────────────────────────────────


async def test_request_join_remote_requires_confirmed_peer(stack):
    stack.fed_repo.get_instance.return_value = None
    with pytest.raises(SpacePermissionError):
        await stack.svc.request_join_remote(
            "sp-remote",
            applicant_user_id="u",
            host_instance_id="unknown-peer",
        )


async def test_request_join_remote_happy(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
        space_type=SpaceType.PUBLIC,
        lat=52.37,
        lon=4.89,
        radius_km=50,
    )
    rid = await stack.svc.request_join_remote(
        space.id,
        applicant_user_id="u-applicant",
        host_instance_id="peer",
        message="join",
    )
    assert rid
    stack.fed_svc.send_event.assert_awaited()


# ── on_remote_join_request_approved ────────────────────────────────


async def test_on_remote_join_request_approved_unknown_noop(stack):
    # No row for this request_id — the handler silently returns.
    await stack.svc.on_remote_join_request_approved(
        "missing",
        invite_token="x",
    )


async def test_on_remote_join_request_approved_happy(stack):
    await _user(stack, "alicehost")
    bob = await _user(stack, "bobapp")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
        space_type=SpaceType.PUBLIC,
        lat=52.37,
        lon=4.89,
        radius_km=50,
    )
    # First create the remote request (seeds space_join_requests row).
    rid = await stack.svc.request_join_remote(
        space.id,
        applicant_user_id=bob.user_id,
        host_instance_id="peer",
    )
    # Mint a token to consume.
    token = await stack.svc.create_invite_token(
        space.id,
        actor_username="alicehost",
    )
    # Handler auto-consumes it.
    await stack.svc.on_remote_join_request_approved(
        rid,
        invite_token=token,
    )


# ── approve_join_request / deny_join_request ──────────────────────


async def test_deny_local_join_request(stack):
    await _user(stack, "alicehost")
    bob = await _user(stack, "bobrequester")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
        join_mode=JoinMode.REQUEST,
    )
    rid = await stack.svc.request_join(
        space.id,
        user_id=bob.user_id,
        message="please",
    )
    await stack.svc.deny_join_request(rid, actor_username="alicehost")
    assert (await stack.space_repo.get_member(space.id, bob.user_id)) is None


async def test_approve_local_join_request(stack):
    await _user(stack, "alicehost")
    bob = await _user(stack, "bobrequester")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
        join_mode=JoinMode.REQUEST,
    )
    rid = await stack.svc.request_join(
        space.id,
        user_id=bob.user_id,
    )
    member = await stack.svc.approve_join_request(
        rid,
        actor_username="alicehost",
    )
    assert member is not None
    assert member.user_id == bob.user_id


async def test_approve_unknown_request_raises(stack):
    await _user(stack, "alicehost")
    await stack.svc.create_space(owner_username="alicehost", name="S")
    with pytest.raises(KeyError):
        await stack.svc.approve_join_request(
            "missing-rid",
            actor_username="alicehost",
        )


# ── mesh-aware delegation for the private-invite family ────────────
#
# Post-refactor, the SpaceService private-invite family delegates to
# ``FederationService.send_with_mesh_fallback`` — the fed service
# decides direct vs mesh. These tests pin the SpaceService contract:
# the right payload reaches the helper, and a failed DeliveryResult
# surfaces as :class:`SpacePermissionError`. The federation-side
# tests cover the direct-vs-mesh branching itself.


async def test_invite_remote_user_uses_mesh_fallback_helper(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="Private",
        space_type=SpaceType.PRIVATE,
    )
    token = await stack.svc.invite_remote_user(
        space.id,
        actor_username="alicehost",
        invitee_instance_id="peer",
        invitee_user_id="bob",
    )
    assert token
    stack.fed_svc.send_with_mesh_fallback.assert_awaited_once()
    call = stack.fed_svc.send_with_mesh_fallback.call_args
    assert call.kwargs["to_instance_id"] == "peer"
    assert call.kwargs["event_type"] == FederationEventType.SPACE_PRIVATE_INVITE
    assert call.kwargs["payload"]["invitee_user_id"] == "bob"
    assert call.kwargs["payload"]["invite_token"] == token


async def test_accept_remote_invite_uses_mesh_fallback_helper(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
    )
    await stack.space_repo.save_remote_invitation(
        space_id=space.id,
        invited_by="alicehost-id",
        remote_instance_id="peer",
        remote_user_id="bob",
        invite_token="tok-accept-mesh",
        space_display_hint="S",
    )
    await stack.svc.accept_remote_invite(
        token="tok-accept-mesh",
        user_id="bob",
    )
    stack.fed_svc.send_with_mesh_fallback.assert_awaited_once()
    call = stack.fed_svc.send_with_mesh_fallback.call_args
    assert call.kwargs["event_type"] == FederationEventType.SPACE_PRIVATE_INVITE_ACCEPT


async def test_decline_remote_invite_uses_mesh_fallback_helper(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
    )
    await stack.space_repo.save_remote_invitation(
        space_id=space.id,
        invited_by="alicehost-id",
        remote_instance_id="peer",
        remote_user_id="bob",
        invite_token="tok-decline-mesh",
        space_display_hint="S",
    )
    await stack.svc.decline_remote_invite(
        token="tok-decline-mesh",
        user_id="bob",
    )
    stack.fed_svc.send_with_mesh_fallback.assert_awaited_once()
    call = stack.fed_svc.send_with_mesh_fallback.call_args
    assert call.kwargs["event_type"] == FederationEventType.SPACE_PRIVATE_INVITE_DECLINE


async def test_remove_remote_member_uses_mesh_fallback_helper(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
    )
    await stack.svc.remove_remote_member(
        space.id,
        actor_username="alicehost",
        instance_id="peer",
        user_id="bob",
    )
    stack.fed_svc.send_with_mesh_fallback.assert_awaited_once()
    call = stack.fed_svc.send_with_mesh_fallback.call_args
    assert call.kwargs["event_type"] == FederationEventType.SPACE_REMOTE_MEMBER_REMOVED


async def test_invite_remote_user_raises_when_fed_returns_no_route(stack):
    """Federation helper returning ok=False (no_route) surfaces as a
    SpacePermissionError so the route layer returns 4xx rather than 200.
    """
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="Private",
        space_type=SpaceType.PRIVATE,
    )
    stack.fed_svc.send_with_mesh_fallback.return_value = DeliveryResult(
        instance_id="peer",
        ok=False,
        error="no_route",
    )
    with pytest.raises(SpacePermissionError):
        await stack.svc.invite_remote_user(
            space.id,
            actor_username="alicehost",
            invitee_instance_id="peer",
            invitee_user_id="bob",
        )
    stack.fed_svc.send_with_mesh_fallback.assert_awaited_once()


# ── set_remote_member_role (#114) ───────────────────────────────────


async def test_set_remote_member_role_promotes_and_broadcasts(stack):
    """Owner promotes a remote member to admin → SQL row flips +
    SPACE_MEMBER_ROLE_CHANGED federates to every member household."""
    stack.fed_svc.broadcast_to_space_members = AsyncMock()
    _alice = await _user(stack, "alicehost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")
    # Seat a remote member directly via the repo.
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="peer-bob",
        user_id="bob",
        user_pk=None,
        display_name="Bob",
    )

    await stack.svc.set_remote_member_role(
        space.id,
        actor_username="alicehost",
        instance_id="peer-bob",
        user_id="bob",
        role="admin",
    )

    member = await stack.svc._remote_members.get(space.id, "peer-bob", "bob")
    assert member is not None
    assert member.role == "admin"
    stack.fed_svc.broadcast_to_space_members.assert_awaited_once()
    args = stack.fed_svc.broadcast_to_space_members.call_args
    assert args.args[0] == space.id
    assert args.args[1] is FederationEventType.SPACE_MEMBER_ROLE_CHANGED
    assert args.args[2]["role"] == "admin"
    assert args.args[2]["instance_id"] == "peer-bob"
    assert args.args[2]["user_id"] == "bob"


async def test_set_remote_member_role_rejects_owner_role(stack):
    _a = await _user(stack, "alicehost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="peer",
        user_id="u",
        user_pk=None,
        display_name=None,
    )
    with pytest.raises(ValueError):
        await stack.svc.set_remote_member_role(
            space.id,
            actor_username="alicehost",
            instance_id="peer",
            user_id="u",
            role="owner",
        )


async def test_set_remote_member_role_requires_owner(stack):
    """Non-owners get a permission error — same as the local
    set_role path."""
    _a = await _user(stack, "alicehost")
    bob = await _user(stack, "bobhost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")
    # Promote bob to admin to verify even admin can't make this call.
    await stack.svc.add_member(
        space.id,
        actor_username="alicehost",
        user_id=bob.user_id,
        role="admin",
    )
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="peer",
        user_id="u",
        user_pk=None,
        display_name=None,
    )
    with pytest.raises(SpacePermissionError):
        await stack.svc.set_remote_member_role(
            space.id,
            actor_username="bobhost",
            instance_id="peer",
            user_id="u",
            role="admin",
        )


async def test_set_remote_member_role_idempotent_skips_broadcast(stack):
    """Setting the role to its current value is a no-op — no
    broadcast, no config-sequence bump."""
    stack.fed_svc.broadcast_to_space_members = AsyncMock()
    _a = await _user(stack, "alicehost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="peer",
        user_id="u",
        user_pk=None,
        display_name=None,
    )
    # Default role is 'member'; setting again to 'member' is a no-op.
    await stack.svc.set_remote_member_role(
        space.id,
        actor_username="alicehost",
        instance_id="peer",
        user_id="u",
        role="member",
    )
    stack.fed_svc.broadcast_to_space_members.assert_not_awaited()


async def test_set_remote_member_role_missing_member_raises(stack):
    _a = await _user(stack, "alicehost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")
    with pytest.raises(KeyError):
        await stack.svc.set_remote_member_role(
            space.id,
            actor_username="alicehost",
            instance_id="ghost",
            user_id="nobody",
            role="admin",
        )


# ── apply_remote_admin_kick + remote-space kick routing (#114 phase 2) ──


async def _seat_remote_stub(stack, *, space_id, user_id, role):
    """Build a stub space hosted on someone else + seat our local
    user with the given role."""
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceMember,
        SpaceType,
    )

    stub = Space(
        id=space_id,
        name="Hosted Elsewhere",
        owner_instance_id="instance-remote-host",
        owner_username="bob@remotehost",
        identity_public_key="",
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
        emoji="🏠",
        description="",
    )
    await stack.space_repo.save(stub)
    await stack.space_repo.save_member(
        SpaceMember(
            space_id=space_id,
            user_id=user_id,
            role=role,
            joined_at="2026-05-23T00:00:00+00:00",
        )
    )
    return stub


async def test_remove_member_on_remote_space_routes_to_host(stack):
    """When the local user kicks someone in a space hosted on another
    household, the kick MUST forward to the host as
    SPACE_REMOTE_ADMIN_KICK instead of mutating the local stub."""
    alice = await _user(stack, "alicehost")
    await _seat_remote_stub(
        stack,
        space_id="sp-remote",
        user_id=alice.user_id,
        role="admin",
    )

    await stack.svc.remove_member(
        "sp-remote",
        actor_username="alicehost",
        user_id="u-victim",
    )

    stack.fed_svc.send_with_mesh_fallback.assert_awaited_once()
    call = stack.fed_svc.send_with_mesh_fallback.call_args
    assert call.kwargs["to_instance_id"] == "instance-remote-host"
    assert call.kwargs["event_type"] is FederationEventType.SPACE_REMOTE_ADMIN_KICK
    assert call.kwargs["payload"]["actor_user_id"] == alice.user_id
    assert call.kwargs["payload"]["target_user_id"] == "u-victim"
    assert call.kwargs["payload"]["actor_instance_id"] == stack.svc._own_instance_id


async def test_remove_member_self_on_remote_space_stays_local(stack):
    """Self-leave on a remote space still runs the local path —
    user is dropping their own stub membership, not asking the host
    to kick anyone."""
    alice = await _user(stack, "alicehost")
    await _seat_remote_stub(
        stack,
        space_id="sp-remote-2",
        user_id=alice.user_id,
        role="member",
    )

    await stack.svc.remove_member(
        "sp-remote-2",
        actor_username="alicehost",
        user_id=alice.user_id,
    )
    sent_types = [
        c.kwargs.get("event_type")
        for c in stack.fed_svc.send_with_mesh_fallback.call_args_list
    ]
    assert FederationEventType.SPACE_REMOTE_ADMIN_KICK not in sent_types


async def test_apply_remote_admin_kick_admin_dispatches_remote_member_remove(stack):
    """Host receives SPACE_REMOTE_ADMIN_KICK from a legitimate admin
    → dispatches to remove_remote_member (target was on a different
    household than the actor)."""
    _alice = await _user(stack, "alicehost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")

    # Seat two remote members. Actor is admin on instance-A; target
    # is a regular member on instance-C.
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="instance-A",
        user_id="u-admin-on-A",
        user_pk=None,
        display_name=None,
    )
    await stack.svc._remote_members.set_role(
        space.id,
        "instance-A",
        "u-admin-on-A",
        "admin",
    )
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="instance-C",
        user_id="u-victim",
        user_pk=None,
        display_name=None,
    )

    stack.fed_svc.broadcast_to_space_members = AsyncMock()

    await stack.svc.apply_remote_admin_kick(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin-on-A",
        target_user_id="u-victim",
    )
    # Victim's row is gone.
    assert (
        await stack.svc._remote_members.get(space.id, "instance-C", "u-victim")
    ) is None


async def test_apply_remote_admin_kick_non_admin_silently_drops(stack):
    """Actor with role='member' (not admin) → dropped. No mutation,
    no exception."""
    _a = await _user(stack, "alicehost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="instance-A",
        user_id="u-not-an-admin",
        user_pk=None,
        display_name=None,
    )
    # Add a victim so we can detect if the kick mistakenly ran.
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="instance-C",
        user_id="u-victim",
        user_pk=None,
        display_name=None,
    )

    await stack.svc.apply_remote_admin_kick(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-not-an-admin",
        target_user_id="u-victim",
    )
    # Victim is still there.
    assert (
        await stack.svc._remote_members.get(space.id, "instance-C", "u-victim")
    ) is not None


async def test_apply_remote_admin_kick_target_owner_rejected(stack):
    """Owner cannot be kicked through this path — same invariant
    as remove_member."""
    alice = await _user(stack, "alicehost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="instance-A",
        user_id="u-admin",
        user_pk=None,
        display_name=None,
    )
    await stack.svc._remote_members.set_role(
        space.id,
        "instance-A",
        "u-admin",
        "admin",
    )

    await stack.svc.apply_remote_admin_kick(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        target_user_id=alice.user_id,
    )
    # Owner row still present.
    assert (await stack.space_repo.get_member(space.id, alice.user_id)) is not None


async def test_apply_remote_admin_kick_unknown_space_drops(stack):
    """A kick for an unknown space drops silently — common after
    SPACE_DISSOLVED races with a stale outbox."""
    await stack.svc.apply_remote_admin_kick(
        "sp-nonexistent",
        actor_instance_id="instance-A",
        actor_user_id="u",
        target_user_id="t",
    )  # Should not raise.
