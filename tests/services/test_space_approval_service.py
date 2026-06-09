"""Tests for :class:`SpaceApprovalService` — multi-admin quorum approval.

Real SQLite repos for state; the *executed* SpaceService and the
federation transport are mocked so we assert the approval logic
(threshold math, reject-cancels, expiry, forward, host dispatch) in
isolation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import (
    SpaceMember,
    SpacePermissionError,
    SpaceRole,
    SpaceType,
)
from socialhome.domain.space_proposal import ProposalAction, ProposalStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_proposal_repo import SqliteSpaceProposalRepo
from socialhome.repositories.space_remote_member_repo import SqliteSpaceRemoteMemberRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_approval_service import SpaceApprovalService
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
    from socialhome.infrastructure.key_manager import KeyManager

    user_repo = SqliteUserRepo(db)
    space_repo = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0c" * 32))
    remote_repo = SqliteSpaceRemoteMemberRepo(db)
    proposal_repo = SqliteSpaceProposalRepo(db)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    # Real SpaceService only to *create* spaces / seat members in setup.
    setup_svc = SpaceService(
        space_repo, SqliteSpacePostRepo(db), user_repo, bus, own_instance_id=iid
    )
    # The approval service executes through a mock so we assert the call
    # without running the real dissolve / update_config machinery.
    exec_svc = MagicMock()
    exec_svc.dissolve_space = AsyncMock()
    exec_svc.update_config = AsyncMock()
    fed = MagicMock()
    fed.send_with_mesh_fallback = AsyncMock()
    fed.broadcast_to_space_members = AsyncMock()
    fed.peer_supports = AsyncMock(return_value=True)
    approvals = SpaceApprovalService(
        proposal_repo, space_repo, remote_repo, user_repo, bus, own_instance_id=iid
    )
    approvals.attach(federation_service=fed, space_service=exec_svc)

    class S:
        pass

    s = S()
    s.db = db
    s.iid = iid
    s.bus = bus
    s.user_svc = user_svc
    s.setup = setup_svc
    s.space_repo = space_repo
    s.remote = remote_repo
    s.proposals = proposal_repo
    s.exec = exec_svc
    s.fed = fed
    s.approvals = approvals
    yield s
    await db.shutdown()


async def _user(stack, name):
    return await stack.user_svc.provision(username=name, display_name=name)


async def _space(stack, owner="alice"):
    await _user(stack, owner)
    return await stack.setup.create_space(owner_username=owner, name="S")


async def _add_admin(stack, space_id, name):
    u = await _user(stack, name)
    await stack.space_repo.save_member(
        SpaceMember(
            space_id=space_id,
            user_id=u.user_id,
            role=SpaceRole.ADMIN,
            joined_at="2026-06-01T00:00:00+00:00",
        )
    )
    return u


# ── solo-admin: immediate execution ─────────────────────────────────


async def test_solo_owner_dissolve_executes_immediately(stack):
    space = await _space(stack)
    view = await stack.approvals.propose(
        space.id, actor_username="alice", action=ProposalAction.DISSOLVE
    )
    assert view["status"] == ProposalStatus.EXECUTED.value
    stack.exec.dissolve_space.assert_awaited_once()


async def test_solo_owner_set_tier_executes_immediately(stack):
    space = await _space(stack)
    view = await stack.approvals.propose(
        space.id,
        actor_username="alice",
        action=ProposalAction.SET_PUBLIC_TIER,
        params={"space_type": "public"},
    )
    assert view["status"] == ProposalStatus.EXECUTED.value
    stack.exec.update_config.assert_awaited_once()
    assert stack.exec.update_config.call_args.kwargs["space_type"] == "public"


# ── two admins: needs both ───────────────────────────────────────────


async def test_two_admins_need_both_to_dissolve(stack):
    space = await _space(stack)
    await _add_admin(stack, space.id, "bob")
    # Alice proposes — pending, not executed (majority of 2 is 2).
    view = await stack.approvals.propose(
        space.id, actor_username="alice", action=ProposalAction.DISSOLVE
    )
    assert view["status"] == ProposalStatus.PENDING.value
    assert view["approvals"] == 1
    assert view["total_admins"] == 2
    assert view["needed"] == 2
    stack.exec.dissolve_space.assert_not_awaited()
    # Bob approves → majority → executes.
    out = await stack.approvals.vote(
        space.id, view["id"], actor_username="bob", approve=True
    )
    assert out["status"] == ProposalStatus.EXECUTED.value
    stack.exec.dissolve_space.assert_awaited_once()


async def test_reject_cancels_proposal(stack):
    space = await _space(stack)
    await _add_admin(stack, space.id, "bob")
    view = await stack.approvals.propose(
        space.id, actor_username="alice", action=ProposalAction.DISSOLVE
    )
    out = await stack.approvals.vote(
        space.id, view["id"], actor_username="bob", approve=False
    )
    assert out["status"] == ProposalStatus.REJECTED.value
    stack.exec.dissolve_space.assert_not_awaited()


async def test_three_admins_majority_is_two(stack):
    space = await _space(stack)
    await _add_admin(stack, space.id, "bob")
    await _add_admin(stack, space.id, "carol")
    view = await stack.approvals.propose(
        space.id, actor_username="alice", action=ProposalAction.DISSOLVE
    )
    assert view["needed"] == 2 and view["total_admins"] == 3
    out = await stack.approvals.vote(
        space.id, view["id"], actor_username="bob", approve=True
    )
    assert out["status"] == ProposalStatus.EXECUTED.value


# ── authorization ────────────────────────────────────────────────────


async def test_non_admin_cannot_propose(stack):
    space = await _space(stack)
    member = await _user(stack, "mallory")
    await stack.space_repo.save_member(
        SpaceMember(
            space_id=space.id,
            user_id=member.user_id,
            role=SpaceRole.MEMBER,
            joined_at="2026-06-01T00:00:00+00:00",
        )
    )
    with pytest.raises(SpacePermissionError):
        await stack.approvals.propose(
            space.id, actor_username="mallory", action=ProposalAction.DISSOLVE
        )


async def test_vote_by_non_admin_dropped(stack):
    space = await _space(stack)
    await _add_admin(stack, space.id, "bob")
    view = await stack.approvals.propose(
        space.id, actor_username="alice", action=ProposalAction.DISSOLVE
    )
    # A plain member tries to vote — _require_local_admin rejects.
    plain = await _user(stack, "mallory")
    await stack.space_repo.save_member(
        SpaceMember(
            space_id=space.id,
            user_id=plain.user_id,
            role=SpaceRole.MEMBER,
            joined_at="2026-06-01T00:00:00+00:00",
        )
    )
    with pytest.raises(SpacePermissionError):
        await stack.approvals.vote(
            space.id, view["id"], actor_username="mallory", approve=True
        )
    stack.exec.dissolve_space.assert_not_awaited()


# ── dedupe + list + expiry ───────────────────────────────────────────


async def test_proposing_twice_reuses_open_proposal(stack):
    space = await _space(stack)
    await _add_admin(stack, space.id, "bob")
    v1 = await stack.approvals.propose(
        space.id, actor_username="alice", action=ProposalAction.DISSOLVE
    )
    v2 = await stack.approvals.propose(
        space.id, actor_username="alice", action=ProposalAction.DISSOLVE
    )
    assert v1["id"] == v2["id"]
    assert len(await stack.approvals.list_for_space(space.id)) == 1


async def test_expire_due_marks_expired(stack):
    space = await _space(stack)
    await _add_admin(stack, space.id, "bob")
    view = await stack.approvals.propose(
        space.id, actor_username="alice", action=ProposalAction.DISSOLVE
    )
    # Force the row's expiry into the past.
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    await stack.db.enqueue(
        "UPDATE space_admin_proposals SET expires_at=? WHERE id=?",
        (past, view["id"]),
    )
    n = await stack.approvals.expire_due()
    assert n == 1
    assert (await stack.proposals.get(view["id"])).status == ProposalStatus.EXPIRED


# ── cross-household forward + host dispatch ──────────────────────────


async def _remote_stub(stack):
    """A space hosted elsewhere where our local user is a (stub) admin."""
    from socialhome.domain.space import JoinMode, Space, SpaceFeatures

    admin = await _user(stack, "localadmin")
    stub = Space(
        id="sp-remote",
        name="S",
        owner_instance_id="host-instance",
        owner_username="hostowner",
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
            space_id=stub.id,
            user_id=admin.user_id,
            role=SpaceRole.ADMIN,
            joined_at="2026-06-01T00:00:00+00:00",
        )
    )
    return stub


async def test_propose_on_remote_stub_forwards(stack):
    stub = await _remote_stub(stack)
    await stack.approvals.propose(
        stub.id, actor_username="localadmin", action=ProposalAction.DISSOLVE
    )
    stack.fed.send_with_mesh_fallback.assert_awaited_once()
    payload = stack.fed.send_with_mesh_fallback.call_args.kwargs["payload"]
    assert payload["action"] == "propose"
    assert payload["params"]["action"] == "dissolve"
    # Nothing executed locally.
    stack.exec.dissolve_space.assert_not_awaited()


async def test_propose_on_remote_stub_raises_when_host_too_old(stack):
    stack.fed.peer_supports = AsyncMock(return_value=False)
    stub = await _remote_stub(stack)
    with pytest.raises(SpacePermissionError):
        await stack.approvals.propose(
            stub.id, actor_username="localadmin", action=ProposalAction.DISSOLVE
        )


async def test_apply_remote_propose_then_vote_executes(stack):
    """Host receives a remote admin's propose + a second remote admin's
    approve → majority → executes."""
    space = await _space(stack)
    # Seat two remote admins on different households.
    for inst, uid in (("host-A", "ada"), ("host-B", "ben")):
        await stack.remote.add(
            space_id=space.id,
            instance_id=inst,
            user_id=uid,
            user_pk=None,
            display_name=None,
        )
        await stack.remote.set_role(space.id, inst, uid, "admin")
    # Total admins now: alice (owner) + ada + ben = 3 → majority 2.
    await stack.approvals.apply_remote_propose(
        space.id,
        proposer_instance="host-A",
        proposer_user="ada",
        action="dissolve",
        params={},
    )
    open_props = await stack.approvals.list_for_space(space.id)
    assert len(open_props) == 1
    pid = open_props[0]["id"]
    stack.exec.dissolve_space.assert_not_awaited()
    await stack.approvals.apply_remote_vote(
        space.id,
        voter_instance="host-B",
        voter_user="ben",
        proposal_id=pid,
        approve=True,
    )
    stack.exec.dissolve_space.assert_awaited_once()


async def test_apply_remote_propose_by_non_admin_dropped(stack):
    space = await _space(stack)
    # A household that is NOT a seated admin tries to propose.
    await stack.approvals.apply_remote_propose(
        space.id,
        proposer_instance="rando-instance",
        proposer_user="rando",
        action="dissolve",
        params={},
    )
    assert await stack.approvals.list_for_space(space.id) == []


# ── member-household mirror shows the host's exact tally (fast-follow) ──


async def test_mirror_update_shows_host_tally_not_local_recompute(stack):
    """A member household stores + returns the host's authoritative view
    verbatim, even when its own roster would compute a different tally."""
    from socialhome.domain.space import JoinMode, Space, SpaceFeatures

    # A stub of a space hosted elsewhere — locally we know of NO admins,
    # so a local recompute would say 0 total. The host's view says 2-of-3.
    stub = Space(
        id="sp-remote",
        name="S",
        owner_instance_id="host-instance",
        owner_username="hostowner",
        identity_public_key="",
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
        emoji="🏠",
        description="",
    )
    await stack.space_repo.save(stub)
    host_view = {
        "id": "prop-1",
        "space_id": stub.id,
        "action": "dissolve",
        "params": {},
        "status": "pending",
        "proposed_by_instance": "host-instance",
        "proposed_by_user": "hostadmin",
        "approvals": 2,
        "total_admins": 3,
        "needed": 2,
        # Relative dates: ``list_for_space`` filters out proposals whose
        # ``expires_at`` is in the past (service.py ``_now()`` check), so a
        # hardcoded date silently rots the test the day it lapses. Keep the
        # proposal active by anchoring on the run time.
        "created_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
    }
    await stack.approvals.apply_mirror_update(stub.id, host_view)
    listed = await stack.approvals.list_for_space(stub.id)
    assert len(listed) == 1
    assert listed[0]["approvals"] == 2
    assert listed[0]["total_admins"] == 3
    assert listed[0]["needed"] == 2


async def test_mirror_update_drops_resolved_proposal(stack):
    """A resolved (executed/rejected) host view removes the mirror row."""
    from socialhome.domain.space import JoinMode, Space, SpaceFeatures

    stub = Space(
        id="sp-remote2",
        name="S",
        owner_instance_id="host-instance",
        owner_username="hostowner",
        identity_public_key="",
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
        emoji="🏠",
        description="",
    )
    await stack.space_repo.save(stub)
    base = {
        "id": "prop-2",
        "space_id": stub.id,
        "action": "dissolve",
        "params": {},
        "proposed_by_instance": "host-instance",
        "proposed_by_user": "hostadmin",
        "approvals": 1,
        "total_admins": 2,
        "needed": 2,
        # Relative dates: ``list_for_space`` filters out proposals whose
        # ``expires_at`` is in the past (service.py ``_now()`` check), so a
        # hardcoded date silently rots the test the day it lapses. Keep the
        # proposal active by anchoring on the run time.
        "created_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
    }
    await stack.approvals.apply_mirror_update(stub.id, {**base, "status": "pending"})
    assert len(await stack.approvals.list_for_space(stub.id)) == 1
    await stack.approvals.apply_mirror_update(stub.id, {**base, "status": "rejected"})
    assert await stack.approvals.list_for_space(stub.id) == []
