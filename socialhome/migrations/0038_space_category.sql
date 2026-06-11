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
--    polling, so it is recreated (not rebuilt) with a ``category`` column;
--    its ``target_audience`` column is retained (the upsert path still uses
--    it), only the over-restrictive ``target_audience`` CHECK is dropped.

ALTER TABLE spaces ADD COLUMN category TEXT;

-- Recreate the public-space cache to add a ``category`` column and drop the
-- legacy ``target_audience`` CHECK (it ingests remote values that may not
-- match our taxonomy — a restrictive CHECK would reject otherwise-valid
-- remote listings). ``target_audience`` itself is RETAINED (the
-- ``public_space_repo`` upsert/read path still threads it) — only its CHECK
-- is dropped. Safe to recreate: the cache repopulates from GFS polling.
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
    -- Retained from 0001, but the original CHECK(target_audience IN
    -- ('all','family','teen','adult')) is intentionally dropped here.
    target_audience TEXT NOT NULL DEFAULT 'all',
    -- Discovery category (free-form here; the taxonomy + normalization live
    -- in ``services.space_service``). No CHECK: this cache ingests remote
    -- values that may not match our taxonomy.
    category        TEXT,
    cached_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
