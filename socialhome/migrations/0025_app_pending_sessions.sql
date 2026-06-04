-- Social Home Apps: durable store for pending app-session invites so an
-- invite to an offline recipient survives until they next open the app.
--
-- Migration audit (CLAUDE.md "Before adding a SQL migration"):
--
-- 1. Audited existing shape: installed_apps is one row per app (no per-user,
--    per-session payload); app_kv is per-(app, user, key) storage but it is
--    app-owned — the app writes/reads its own keys, so the server cannot
--    durably stash an inbound invite there without colliding with the app's
--    namespace; notifications holds the bell-domain feed and is not
--    app-readable. None of the three carry an APP_SESSION invite payload.
--
-- 2. Non-migration alternatives considered and rejected:
--    * app_kv: it is the app's own key space (quota-accounted, server never
--      writes it on the app's behalf); stashing routing/invite state there
--      blurs ownership and risks key collisions.
--    * notifications: bell-domain, not exposed to the app runtime; an invite
--      is not a notification row and the app could never read it back.
--    * Hold in memory / a federation re-send: an invite to an offline user
--      must survive a process restart and need not depend on the sender
--      retrying, so there must be an on-disk home.
--    A dedicated per-(app, user, session) table is the only shape that gives
--    durable, app-scoped invite storage with FK-cascade cleanup on uninstall
--    and on user deletion.
--
-- 3. Smallest additive change: a single CREATE TABLE with the two cascades
--    plus one supporting index; no existing row or table touched.

CREATE TABLE app_pending_sessions (
    app_id        TEXT NOT NULL REFERENCES installed_apps(app_id) ON DELETE CASCADE,
    user_id       TEXT NOT NULL REFERENCES users(user_id)          ON DELETE CASCADE,
    session_id    TEXT NOT NULL,
    from_instance TEXT NOT NULL,
    from_user     TEXT,                 -- remote sender's user ref (may be null pre-v18 routing)
    payload_json  TEXT NOT NULL,        -- JSON-encoded APP_SESSION payload
    created_at    TEXT NOT NULL,        -- UTC ISO 8601
    PRIMARY KEY (app_id, user_id, session_id)
);

CREATE INDEX idx_app_pending_sessions_app_user
    ON app_pending_sessions(app_id, user_id, created_at);
