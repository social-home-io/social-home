-- Cross-household admin promotion (#114, PR #434).
--
-- Audit per the CLAUDE.md "Before adding a SQL migration" rule:
--
-- 1. Existing code paths touching this data — only the
--    SqliteSpaceRemoteMemberRepo (insert / delete / list) and the
--    private-invite handlers that seat new remote members. Neither
--    expressed a role concept previously; remote members were
--    implicit "members" forever.
-- 2. Non-migration alternative considered — could we ship the role
--    on every federation event a remote member participates in,
--    reading the latest one as ground truth? Rejected: the role
--    needs to be queryable at REST time (POST /api/spaces/{id}/
--    promote, the /members listing endpoint) without a federation
--    round-trip, and the source-of-truth model would race with
--    the host's local view. A real column is the right home.
-- 3. Minimality — additive ``ADD COLUMN`` with a NOT NULL DEFAULT.
--    Existing rows transparently become role='member'. The CHECK
--    constraint mirrors the on-disk authority pattern from
--    ``space_members`` (role IN ('owner','admin','member','subscriber')).
--    Owner is intentionally OMITTED for the remote case — ownership
--    cannot be held by a remote member (transfer is a local-only
--    operation gated on ``_require_owner``); subscriber is also
--    omitted because subscriber-role members never have a
--    ``space_remote_members`` row (subscriptions are tracked in a
--    different table).
ALTER TABLE space_remote_members
    ADD COLUMN role TEXT NOT NULL DEFAULT 'member'
    CHECK(role IN ('member', 'admin'));
