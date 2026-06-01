"""Domain types for multi-admin approval of critical space actions.

A :class:`SpaceAdminProposal` is a pending high-stakes action (dissolve
the space, or change its publication tier) that executes only once a
*majority* of the space's admins approve it — the owner included. Pure
row-shaped dataclasses; no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProposalAction(StrEnum):
    """Critical actions that require majority-admin approval."""

    #: Permanent delete of the space + all content (was owner-only).
    DISSOLVE = "dissolve"
    #: Change the publication tier (``space_type`` → public / global /
    #: private), which advertises the space or auto-publishes it to GFS.
    SET_PUBLIC_TIER = "set_public_tier"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    EXECUTED = "executed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ProposalVote(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(slots=True, frozen=True)
class SpaceAdminProposal:
    """A pending / resolved critical-action proposal (a ``space_admin_proposals`` row)."""

    id: str
    space_id: str
    action: ProposalAction
    params: dict
    proposed_by_instance: str
    proposed_by_user: str
    status: ProposalStatus
    created_at: str
    expires_at: str
    #: On a member-household *mirror* row, the host's authoritative SPA view
    #: (tally included), so the member shows the host's exact numbers rather
    #: than recomputing from its own roster. ``None`` on the host (it
    #: recomputes the view authoritatively).
    host_view: dict | None = None


@dataclass(slots=True, frozen=True)
class SpaceAdminProposalVote:
    """One admin's vote on a proposal (a ``space_admin_proposal_votes`` row)."""

    proposal_id: str
    voter_instance: str
    voter_user: str
    vote: ProposalVote
    voted_at: str
