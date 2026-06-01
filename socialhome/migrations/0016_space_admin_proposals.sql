-- Multi-admin approval (quorum) for critical space actions (#114 follow-on).
--
-- Some space actions are too high-stakes for a single admin to perform
-- alone: dissolving the space (permanent delete) and changing its
-- publication tier (public / global → advertised / GFS-published). These
-- become *proposals* that execute only once a MAJORITY of the space's
-- admins approve — the owner is bound by the same rule (no single person,
-- not even the owner, can unilaterally nuke or publish the group).
--
-- Migration audit (CLAUDE.md):
--   1. Audited the existing shape — this is genuinely new workflow state
--      (an approval ledger), not derivable from the space row, a federation
--      event, or any existing column. There is no existing home for "who
--      has approved this pending dissolve".
--   2. Non-migration alternatives rejected — computing at read time is
--      impossible (votes are durable facts); stuffing it into space_meta /
--      a config event would conflate transient workflow with persistent
--      config and re-broadcast on every refresh.
--   3. Smallest possible change — two additive CREATE TABLEs, no change to
--      existing rows, no backfill. Both reference spaces(id) ON DELETE
--      CASCADE so a dissolve / purge cleans them up with the content graph.
--
-- These tables exist on the host (authoritative) AND on member households
-- (a read-only mirror kept in sync by SPACE_ADMIN_PROPOSAL_UPDATED) so a
-- remote admin's SPA can render the pending proposal + tally and vote. The
-- host is always the source of truth; votes forward to it and it
-- re-broadcasts the updated state.

CREATE TABLE IF NOT EXISTS space_admin_proposals (
    id                    TEXT PRIMARY KEY,
    space_id              TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    -- 'dissolve' | 'set_public_tier'
    action                TEXT NOT NULL,
    -- JSON params for the action (e.g. {"space_type": "public"}); '{}' for dissolve
    params_json           TEXT NOT NULL DEFAULT '{}',
    proposed_by_instance  TEXT NOT NULL,
    proposed_by_user      TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending', 'executed', 'rejected', 'expired')),
    created_at            TEXT NOT NULL,
    expires_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_space_admin_proposals_space
    ON space_admin_proposals(space_id, status);

CREATE TABLE IF NOT EXISTS space_admin_proposal_votes (
    proposal_id     TEXT NOT NULL REFERENCES space_admin_proposals(id) ON DELETE CASCADE,
    voter_instance  TEXT NOT NULL,
    voter_user      TEXT NOT NULL,
    vote            TEXT NOT NULL CHECK (vote IN ('approve', 'reject')),
    voted_at        TEXT NOT NULL,
    PRIMARY KEY (proposal_id, voter_instance, voter_user)
);
