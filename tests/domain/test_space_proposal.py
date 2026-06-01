"""Tests for the space-proposal domain types."""

from __future__ import annotations

import pytest

from socialhome.domain.space_proposal import (
    ProposalAction,
    ProposalStatus,
    ProposalVote,
    SpaceAdminProposal,
    SpaceAdminProposalVote,
)


def test_enum_values():
    assert ProposalAction.DISSOLVE.value == "dissolve"
    assert ProposalAction.SET_PUBLIC_TIER.value == "set_public_tier"
    assert {s.value for s in ProposalStatus} == {
        "pending",
        "executed",
        "rejected",
        "expired",
    }
    assert {v.value for v in ProposalVote} == {"approve", "reject"}


def test_dataclasses_are_frozen():
    p = SpaceAdminProposal(
        id="p1",
        space_id="s1",
        action=ProposalAction.DISSOLVE,
        params={},
        proposed_by_instance="i",
        proposed_by_user="u",
        status=ProposalStatus.PENDING,
        created_at="2026-06-01T00:00:00+00:00",
        expires_at="2026-06-08T00:00:00+00:00",
    )
    v = SpaceAdminProposalVote(
        proposal_id="p1",
        voter_instance="i",
        voter_user="u",
        vote=ProposalVote.APPROVE,
        voted_at="2026-06-01T00:00:00+00:00",
    )
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        p.status = ProposalStatus.EXECUTED  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.vote = ProposalVote.REJECT  # type: ignore[misc]
