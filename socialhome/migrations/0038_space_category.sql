-- Add a discovery "category" to spaces (and the public-space cache).
--
-- Audit per the CLAUDE.md "Before adding a SQL migration" rule:
--
-- 1. Existing code paths: the only discovery-metadata field today is
--    ``spaces.target_audience`` (an age-band label: all/family/teen/adult)
--    that federates to the public/global directory. It is CHECK-locked at
--    the schema level (``0001_initial.sql`` on both ``spaces`` and
--    ``public_space_cache``) to those four values.
-- 2. Non-migration alternative considered — reuse ``target_audience`` for the
--    new 10-value category taxonomy. Rejected: relaxing its CHECK needs a
--    full rebuild of the 36-column ``spaces`` table (5 FK dependents) — the
--    destructive ``RENAME TABLE`` pattern this rule warns against.
-- 3. Minimality — a nullable ``ADD COLUMN`` is the smallest possible change:
--    no rebuild, no FK risk, no backfill (NULL = unset → displays "general").
--    ``public_space_cache`` is a local cache that repopulates from GFS
--    polling, so it is recreated (not rebuilt) with a ``category`` column and
--    WITHOUT the legacy ``target_audience`` column — ``category`` fully
--    replaces it (no reader survives task 6).

ALTER TABLE spaces ADD COLUMN category TEXT;

-- Recreate the public-space cache with a ``category`` column and drop the
-- legacy ``target_audience`` column entirely (``category`` replaces it as the
-- §23.50 discovery taxonomy — no reader remains). Safe to recreate: the cache
-- repopulates from GFS polling.
DROP TABLE IF EXISTS public_space_cache;
CREATE TABLE IF NOT EXISTS public_space_cache (
    space_id        TEXT PRIMARY KEY,
    instance_id     TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT,
    emoji           TEXT,
    lat             REAL,
    lon             REAL,
    radius_km       REAL,
    member_count    INTEGER NOT NULL DEFAULT 0,
    -- Mirror ``spaces.min_age`` CHECK so a malformed inbound discover entry
    -- cannot quietly land.
    min_age         INTEGER NOT NULL DEFAULT 0
                    CHECK(min_age IN (0, 13, 16, 18)),
    -- Discovery category (§23.50). No CHECK: this cache ingests remote values
    -- that may not match our taxonomy (normalized to "general" on read).
    category        TEXT,
    cached_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
