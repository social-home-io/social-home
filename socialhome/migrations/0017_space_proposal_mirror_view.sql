-- Store the host's authoritative proposal view on member-household mirrors.
--
-- The approval tally (approvals / total_admins / needed) is computed by the
-- HOST against its canonical admin roster. A member household can only
-- approximate it from its own (possibly stale) roster mirror, so the count
-- it showed on an initial REST load could differ from the host's. The host
-- already broadcasts its exact view in SPACE_ADMIN_PROPOSAL_UPDATED; this
-- column persists that view on the mirror row so the member's REST list
-- returns the host's numbers verbatim instead of recomputing them.
--
-- Migration audit (CLAUDE.md):
--   1. Audited the path — the host tally is genuinely not derivable on a
--      member (it depends on the host's full admin set); the only source is
--      the host's broadcast view.
--   2. Alternatives rejected — recomputing locally is the bug being fixed;
--      an in-memory cache wouldn't survive restart and is per-process.
--   3. Smallest change — one additive, NULL-defaulted TEXT column. Host rows
--      leave it NULL (they recompute authoritatively); only mirror rows
--      populate it. No backfill, no table rewrite.

ALTER TABLE space_admin_proposals
    ADD COLUMN host_view_json TEXT;
