-- Social Home Apps: registry of admin-installed embedded JS apps.
--
-- Migration audit (CLAUDE.md):
--   1. Audited existing tables — no registry/extension table exists; the
--      preferences table is scoped to fixed feature toggles, not an open
--      set of installable apps, so it can't host this.
--   2. Alternative rejected — a JSON blob column on `preferences` would
--      conflate admin toggles with an unbounded app list and lose per-app
--      FK-cascade cleanup (needed for PR2's app_kv). A first-class table is
--      the smallest shape that supports cascade-on-uninstall.
--   3. Smallest change — additive CREATE TABLE only; no existing row touched.

CREATE TABLE IF NOT EXISTS installed_apps (
    app_id        TEXT PRIMARY KEY,           -- catalog slug, e.g. 'chess'
    name          TEXT NOT NULL,
    version       TEXT NOT NULL,              -- semver of installed bundle
    enabled       INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    manifest_json TEXT NOT NULL DEFAULT '{}', -- capabilities, entry file, icon
    bundle_path   TEXT NOT NULL,              -- relative dir under media_path/apps/
    bundle_sha256 TEXT NOT NULL,              -- verified at install
    source_url    TEXT NOT NULL,              -- release asset URL it came from
    installed_by  TEXT REFERENCES users(user_id) ON DELETE SET NULL,
    installed_at  TEXT NOT NULL               -- UTC ISO 8601 ('…+00:00')
);
