-- Per-app minimum age (§CP) — child protection blocks under-age users.
-- Audit: additive ADD COLUMN, NULL-safe default 0 (= no restriction); mirrors
-- the spaces.min_age shape. No backfill, no row rewrite.
ALTER TABLE installed_apps ADD COLUMN min_age INTEGER NOT NULL DEFAULT 0
    CHECK (min_age IN (0, 13, 16, 18));
