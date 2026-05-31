-- Space archive (soft, reversible, federated read-only).
--
-- Why a new column rather than reusing ``dissolved``:
--   ``dissolved`` means *gone / inaccessible* — ``SpaceService._require_space``
--   raises "not found" for it, so reads AND writes fail (it's used to hide
--   memberless remote stubs). Archive is the opposite: the space stays
--   *readable*, only writes are blocked and it drops out of active lists.
--   The two states are independent (a space is never both), so they need
--   distinct flags.
--
-- Smallest possible change: a single additive, NULL-free, 0-defaulted
-- column with the same 0/1 CHECK shape as ``dissolved``. No backfill
-- (every existing space is un-archived by definition), no table rewrite.
-- The archived state federates over the existing SPACE_CONFIG_CHANGED +
-- space_meta path, so no new event type or wire migration.
ALTER TABLE spaces
    ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1));
