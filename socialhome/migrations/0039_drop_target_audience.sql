-- Remove the legacy target_audience column now that `category` (0038) replaces
-- it across the codebase (spaces + the two discovery caches).
-- DROP COLUMN is a native ALTER on SQLite >= 3.35 (verified 3.46.1 against the
-- self-CHECK column); requires-python >=3.14 guarantees a SQLite past that floor
-- — no table rebuild.
-- Audit (CLAUDE.md): (1) every target_audience reader/writer migrated to
-- `category` in this change-set (domain.Space, space_repo, public_space_repo,
-- peer_space_directory_repo, the federation producers/consumers, GFS gossip);
-- (2) keeping a vestigial column was rejected — a dead column + dead wire field
-- is worse than removing both, and the drop is cheap; (3) DROP COLUMN is the
-- smallest change to the clean end state.

ALTER TABLE spaces DROP COLUMN target_audience;

-- The peer-public-space directory cache (0001) carried the same legacy hint.
-- It now stores `category` instead; add the column then drop the dead one.
-- A cache repopulated from SPACE_DIRECTORY_SYNC polling — no backfill needed.
ALTER TABLE peer_space_directory ADD COLUMN category TEXT NOT NULL DEFAULT 'general';
ALTER TABLE peer_space_directory DROP COLUMN target_audience;
