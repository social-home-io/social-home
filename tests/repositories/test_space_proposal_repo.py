"""Tests for :class:`SqliteSpaceProposalRepo`."""

from __future__ import annotations

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.domain.space_proposal import (
    ProposalAction,
    ProposalStatus,
    ProposalVote,
    SpaceAdminProposal,
    SpaceAdminProposalVote,
)
from socialhome.repositories.space_proposal_repo import SqliteSpaceProposalRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo


@pytest.fixture
async def repo(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    # A parent space row (FK target).
    await SqliteSpaceRepo(db).save(
        Space(
            id="s1",
            name="S",
            owner_instance_id="i",
            owner_username="o",
            identity_public_key="",
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
            emoji="",
            description="",
        )
    )
    yield SqliteSpaceProposalRepo(db)
    await db.shutdown()


def _p(pid="p1", action=ProposalAction.DISSOLVE, status=ProposalStatus.PENDING):
    return SpaceAdminProposal(
        id=pid,
        space_id="s1",
        action=action,
        params={"space_type": "public"}
        if action == ProposalAction.SET_PUBLIC_TIER
        else {},
        proposed_by_instance="i",
        proposed_by_user="u",
        status=status,
        created_at="2026-06-01T00:00:00+00:00",
        expires_at="2026-06-08T00:00:00+00:00",
    )


async def test_upsert_get_roundtrip(repo):
    await repo.upsert(_p(action=ProposalAction.SET_PUBLIC_TIER))
    got = await repo.get("p1")
    assert got is not None
    assert got.action == ProposalAction.SET_PUBLIC_TIER
    assert got.params == {"space_type": "public"}
    assert got.status == ProposalStatus.PENDING


async def test_find_open_and_list_open(repo):
    await repo.upsert(_p("p1"))
    assert (await repo.find_open("s1", ProposalAction.DISSOLVE)).id == "p1"
    assert await repo.find_open("s1", ProposalAction.SET_PUBLIC_TIER) is None
    assert [p.id for p in await repo.list_open("s1")] == ["p1"]


async def test_set_status_hides_from_open(repo):
    await repo.upsert(_p("p1"))
    await repo.set_status("p1", ProposalStatus.EXECUTED)
    assert await repo.list_open("s1") == []
    assert (await repo.get("p1")).status == ProposalStatus.EXECUTED


async def test_votes_idempotent_and_listed(repo):
    await repo.upsert(_p("p1"))
    v = SpaceAdminProposalVote("p1", "i", "u", ProposalVote.APPROVE, "t")
    await repo.record_vote(v)
    # Re-voting the same (proposal, instance, user) updates, not duplicates.
    await repo.record_vote(
        SpaceAdminProposalVote("p1", "i", "u", ProposalVote.REJECT, "t2")
    )
    votes = await repo.list_votes("p1")
    assert len(votes) == 1
    assert votes[0].vote == ProposalVote.REJECT


async def test_list_expired(repo):
    await repo.upsert(_p("p1"))  # expires 2026-06-08
    assert [p.id for p in await repo.list_expired("2026-06-09T00:00:00+00:00")] == [
        "p1"
    ]
    assert await repo.list_expired("2026-06-01T00:00:00+00:00") == []


async def test_delete_cascades_votes(repo):
    await repo.upsert(_p("p1"))
    await repo.record_vote(
        SpaceAdminProposalVote("p1", "i", "u", ProposalVote.APPROVE, "t")
    )
    await repo.delete("p1")
    assert await repo.get("p1") is None
    assert await repo.list_votes("p1") == []
