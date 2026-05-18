"""Tests for socialhome.repositories.federation_repo."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.federation import (
    PairingSession,
    PairingStatus,
    RemoteInstance,
)
from socialhome.repositories.federation_repo import SqliteFederationRepo


@pytest.fixture
async def env(tmp_dir):
    """Minimal env with a federation repo over a real SQLite database."""
    from socialhome.crypto import generate_identity_keypair, derive_instance_id
    from socialhome.db.database import AsyncDatabase

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.iid = iid
    e.fed_repo = SqliteFederationRepo(db)
    yield e
    await db.shutdown()


async def test_federation_pairing_lifecycle(env):
    """Create, read, update, then delete a pairing session."""
    now = datetime.now(timezone.utc).isoformat()
    session = PairingSession(
        token="tok-abc",
        own_identity_pk="aa" * 32,
        own_dh_pk="bb" * 32,
        own_dh_sk="cc" * 32,
        inbox_url="https://local/inbox/own-id",
        own_local_inbox_id="own-id",
        issued_at=now,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        status=PairingStatus.PENDING_SENT,
    )
    await env.fed_repo.create_pairing(session)

    got = await env.fed_repo.get_pairing("tok-abc")
    assert got is not None
    assert got.token == "tok-abc"
    assert got.status == PairingStatus.PENDING_SENT

    updated_session = PairingSession(
        token="tok-abc",
        own_identity_pk=session.own_identity_pk,
        own_dh_pk=session.own_dh_pk,
        own_dh_sk=session.own_dh_sk,
        inbox_url=session.inbox_url,
        own_local_inbox_id=session.own_local_inbox_id,
        peer_identity_pk="dd" * 32,
        peer_dh_pk="ee" * 32,
        peer_inbox_url="https://peer/inbox",
        issued_at=now,
        expires_at=session.expires_at,
        status=PairingStatus.PENDING_RECEIVED,
    )
    await env.fed_repo.update_pairing(updated_session)
    refreshed = await env.fed_repo.get_pairing("tok-abc")
    assert refreshed.status == PairingStatus.PENDING_RECEIVED
    assert refreshed.peer_inbox_url == "https://peer/inbox"

    await env.fed_repo.delete_pairing("tok-abc")
    assert await env.fed_repo.get_pairing("tok-abc") is None


async def test_cleanup_expired_pairings_no_rows(env):
    """Empty table → returns 0, no side effects."""
    pruned = await env.fed_repo.cleanup_expired_pairings()
    assert pruned == 0


async def test_cleanup_expired_pairings_keeps_fresh_rows(env):
    """A session whose ``expires_at`` is in the future is left alone."""
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    session = PairingSession(
        token="tok-fresh",
        own_identity_pk="aa" * 32,
        own_dh_pk="bb" * 32,
        own_dh_sk="cc" * 32,
        inbox_url="https://local/inbox/own-fresh",
        own_local_inbox_id="own-fresh",
        issued_at=datetime.now(timezone.utc).isoformat(),
        expires_at=future,
        status=PairingStatus.PENDING_SENT,
    )
    await env.fed_repo.create_pairing(session)
    pruned = await env.fed_repo.cleanup_expired_pairings()
    assert pruned == 0
    assert await env.fed_repo.get_pairing("tok-fresh") is not None


async def test_cleanup_expired_pairings_deletes_session_and_orphan_instance(env):
    """An expired session plus its PENDING_RECEIVED orphan instance both
    get pruned. A CONFIRMED instance sharing the local_inbox_id (should
    never happen — defensive guard) is left alone."""
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    session = PairingSession(
        token="tok-expired",
        own_identity_pk="aa" * 32,
        own_dh_pk="bb" * 32,
        own_dh_sk="cc" * 32,
        inbox_url="https://local/inbox/own-stale",
        own_local_inbox_id="own-stale",
        issued_at=past,
        expires_at=past,
        status=PairingStatus.PENDING_RECEIVED,
    )
    await env.fed_repo.create_pairing(session)
    orphan = RemoteInstance(
        id="peer-stale",
        display_name="Stale",
        remote_identity_pk="11" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://stale/wh",
        local_inbox_id="own-stale",
        status=PairingStatus.PENDING_RECEIVED,
    )
    await env.fed_repo.save_instance(orphan)

    pruned = await env.fed_repo.cleanup_expired_pairings()
    assert pruned == 1
    assert await env.fed_repo.get_pairing("tok-expired") is None
    assert await env.fed_repo.get_instance("peer-stale") is None


async def test_cleanup_expired_pairings_preserves_confirmed_instance(env):
    """Defensive: even if an expired session and a CONFIRMED instance
    share a local_inbox_id, the CONFIRMED row stays. (Real flows
    delete the session before flipping the instance to CONFIRMED, so
    this scenario shouldn't arise — but the status filter is the belt
    that protects against a future bug.)"""
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    session = PairingSession(
        token="tok-x",
        own_identity_pk="aa" * 32,
        own_dh_pk="bb" * 32,
        own_dh_sk="cc" * 32,
        inbox_url="https://local/inbox/own-confirmed",
        own_local_inbox_id="own-confirmed",
        issued_at=past,
        expires_at=past,
        status=PairingStatus.PENDING_RECEIVED,
    )
    await env.fed_repo.create_pairing(session)
    inst = RemoteInstance(
        id="peer-confirmed",
        display_name="Real",
        remote_identity_pk="22" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://real/wh",
        local_inbox_id="own-confirmed",
        status=PairingStatus.CONFIRMED,
    )
    await env.fed_repo.save_instance(inst)

    pruned = await env.fed_repo.cleanup_expired_pairings()
    assert pruned == 1
    # Confirmed row survives.
    assert await env.fed_repo.get_instance("peer-confirmed") is not None


async def test_cleanup_expired_pairings_handles_session_without_instance(env):
    """``initiate()`` creates a session but no RemoteInstance row. The
    cleanup must still prune the orphan session even with no peer
    instance to delete."""
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    session = PairingSession(
        token="tok-initiate",
        own_identity_pk="aa" * 32,
        own_dh_pk="bb" * 32,
        own_dh_sk="cc" * 32,
        inbox_url="https://local/inbox/own-initiate",
        own_local_inbox_id="own-initiate",
        issued_at=past,
        expires_at=past,
        status=PairingStatus.PENDING_SENT,
    )
    await env.fed_repo.create_pairing(session)
    pruned = await env.fed_repo.cleanup_expired_pairings()
    assert pruned == 1
    assert await env.fed_repo.get_pairing("tok-initiate") is None


async def test_federation_replay_cache(env):
    """Insert replay IDs and confirm they appear in load_replay_cache; prune works."""
    await env.fed_repo.insert_replay_id("msg-001")
    await env.fed_repo.insert_replay_id("msg-002")

    entries = await env.fed_repo.load_replay_cache(within_hours=1)
    msg_ids = {e[0] for e in entries}
    assert "msg-001" in msg_ids
    assert "msg-002" in msg_ids

    await env.fed_repo.insert_replay_id("msg-001")

    yesterday = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    removed = await env.fed_repo.prune_replay_cache(yesterday)
    assert removed == 0

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    removed_all = await env.fed_repo.prune_replay_cache(future)
    assert removed_all >= 2


async def test_federation_instance_filtering(env):
    """Save two instances, filter by status, mark unreachable/reachable, then delete."""
    inst1 = RemoteInstance(
        id="peer-001",
        display_name="Alpha",
        remote_identity_pk="11" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://alpha/wh",
        local_inbox_id="wh-1",
        status=PairingStatus.CONFIRMED,
    )
    inst2 = RemoteInstance(
        id="peer-002",
        display_name="Beta",
        remote_identity_pk="22" * 32,
        key_self_to_remote="k3",
        key_remote_to_self="k4",
        remote_inbox_url="https://beta/wh",
        local_inbox_id="wh-2",
        status=PairingStatus.UNPAIRING,
    )
    await env.fed_repo.save_instance(inst1)
    await env.fed_repo.save_instance(inst2)

    confirmed = await env.fed_repo.list_instances(status="confirmed")
    confirmed_ids = {i.id for i in confirmed}
    assert "peer-001" in confirmed_ids
    assert "peer-002" not in confirmed_ids

    await env.fed_repo.mark_unreachable("peer-001")
    got = await env.fed_repo.get_instance("peer-001")
    assert not got.is_reachable()

    await env.fed_repo.mark_reachable("peer-001")
    assert (await env.fed_repo.get_instance("peer-001")).is_reachable()

    await env.fed_repo.delete_instance("peer-002")
    assert await env.fed_repo.get_instance("peer-002") is None


async def test_get_instance_by_local_inbox_id_hit(env):
    inst = RemoteInstance(
        id="peer-aa",
        display_name="AA",
        remote_identity_pk="aa" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://aa/wh",
        local_inbox_id="inbox-aa",
        status=PairingStatus.CONFIRMED,
    )
    await env.fed_repo.save_instance(inst)
    got = await env.fed_repo.get_instance_by_local_inbox_id("inbox-aa")
    assert got is not None
    assert got.id == "peer-aa"


async def test_get_instance_by_local_inbox_id_miss(env):
    assert await env.fed_repo.get_instance_by_local_inbox_id("nope") is None


async def test_list_instances_in_space_filters_membership_status_and_bans(env):
    """JOIN excludes non-members, non-confirmed peers, and banned peers."""
    member = RemoteInstance(
        id="peer-mem",
        display_name="Mem",
        remote_identity_pk="aa" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://mem/wh",
        local_inbox_id="wh-mem",
        status=PairingStatus.CONFIRMED,
    )
    outsider = RemoteInstance(
        id="peer-out",
        display_name="Out",
        remote_identity_pk="bb" * 32,
        key_self_to_remote="k3",
        key_remote_to_self="k4",
        remote_inbox_url="https://out/wh",
        local_inbox_id="wh-out",
        status=PairingStatus.CONFIRMED,
    )
    pending = RemoteInstance(
        id="peer-pend",
        display_name="Pend",
        remote_identity_pk="cc" * 32,
        key_self_to_remote="k5",
        key_remote_to_self="k6",
        remote_inbox_url="https://pend/wh",
        local_inbox_id="wh-pend",
        status=PairingStatus.PENDING_SENT,
    )
    banned = RemoteInstance(
        id="peer-ban",
        display_name="Ban",
        remote_identity_pk="dd" * 32,
        key_self_to_remote="k7",
        key_remote_to_self="k8",
        remote_inbox_url="https://ban/wh",
        local_inbox_id="wh-ban",
        status=PairingStatus.CONFIRMED,
    )
    for inst in (member, outsider, pending, banned):
        await env.fed_repo.save_instance(inst)

    # Add a space and seed membership.
    space_id = "sp-1"
    await env.db.enqueue(
        "INSERT INTO spaces(id, name, space_type, owner_instance_id, "
        "owner_username, identity_public_key) "
        "VALUES(?,?,?,?,?,?)",
        (space_id, "Space", "household", env.iid, "owner", "00" * 32),
    )
    for iid in (member.id, pending.id, banned.id):
        await env.db.enqueue(
            "INSERT INTO space_instances(space_id, instance_id) VALUES(?, ?)",
            (space_id, iid),
        )
    await env.fed_repo.ban_instance_from_space(space_id, banned.id)

    got = await env.fed_repo.list_instances_in_space(space_id)
    got_ids = {i.id for i in got}
    assert got_ids == {member.id}
