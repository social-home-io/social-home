-- Replace the legacy ``target_audience`` discovery hint on GFS-published
-- spaces with the §23.50 ``category`` taxonomy.
--
-- Audit per the CLAUDE.md "Before adding a SQL migration" rule:
--
-- 1. Existing code paths: the only discovery-metadata hint on a published
--    space is ``global_spaces.target_audience`` (an age-band label), set from
--    the owner's signed ``publish_space`` body and read into ``GlobalSpace``.
--    Every reader/writer (``repositories.upsert_space`` / ``_row_to_space``,
--    ``federation`` publish, ``cluster`` gossip) is migrated to ``category``
--    in this same change-set.
-- 2. Non-migration alternative considered — keep ``target_audience`` and store
--    the category in it. Rejected: a dead column plus a dead wire field is
--    worse than removing both; the published-space dict now carries
--    ``category`` so the public-page filter (task 6b) can group on it.
-- 3. Minimality — additive ``ADD COLUMN`` for the new value, then a native
--    ``DROP COLUMN`` of the dead one (SQLite >= 3.35; requires-python >=3.14
--    guarantees a SQLite past that floor — no table rebuild).

ALTER TABLE global_spaces ADD COLUMN category TEXT NOT NULL DEFAULT 'general';
ALTER TABLE global_spaces DROP COLUMN target_audience;
