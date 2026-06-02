-- Social Home Apps: per-user key-value store for installed apps.
--
-- Migration audit (CLAUDE.md):
--   1. Audited the PR1 installed_apps shape + the preferences table. Neither
--      holds open-ended per-(app,user,key) data; preferences is a fixed
--      column set, installed_apps is one row per app. A dedicated table is
--      the only shape that gives per-app/per-user namespacing + FK-cascade
--      cleanup on uninstall and on user deletion.
--   2. Alternative rejected — a JSON blob on installed_apps would be
--      household-global (not per-user) and lose key-level reads/writes and
--      quota accounting; a column on preferences can't key by app.
--   3. Smallest change — additive CREATE TABLE with the two cascades; no
--      existing row touched.

CREATE TABLE app_kv (
    app_id     TEXT NOT NULL REFERENCES installed_apps(app_id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES users(user_id)          ON DELETE CASCADE,
    key        TEXT NOT NULL,
    value_json TEXT NOT NULL,        -- JSON-encoded value (any JSON type)
    updated_at TEXT NOT NULL,        -- UTC ISO 8601
    PRIMARY KEY (app_id, user_id, key)
);
