-- GFS schema — Global Federation Server (spec §24.6).
--
-- Greenfield pre-release: this one file carries the complete schema.
-- Rows in `server_config` override TOML values for admin-editable keys.

CREATE TABLE IF NOT EXISTS server_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Admin-editable policy + branding defaults (rows win over TOML at runtime).
INSERT OR IGNORE INTO server_config(key, value) VALUES('server_name',       'My Global Server');
INSERT OR IGNORE INTO server_config(key, value) VALUES('landing_markdown',  '');
INSERT OR IGNORE INTO server_config(key, value) VALUES('header_image_file', '');
INSERT OR IGNORE INTO server_config(key, value) VALUES('auto_accept_clients','1');
INSERT OR IGNORE INTO server_config(key, value) VALUES('auto_accept_spaces', '0');
INSERT OR IGNORE INTO server_config(key, value) VALUES('fraud_threshold',    '5');
-- admin_password_hash row is created when the operator runs --set-password.

-- ── Client instances (spec §24.6) ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS client_instances (
    instance_id  TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    public_key   TEXT NOT NULL,
    inbox_url    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','active','banned')),
    auto_accept  INTEGER NOT NULL DEFAULT 0,
    connected_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Global spaces (spec §24.6) ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS global_spaces (
    space_id         TEXT PRIMARY KEY,
    owning_instance  TEXT NOT NULL REFERENCES client_instances(instance_id),
    name             TEXT NOT NULL DEFAULT '',
    description      TEXT,
    about_markdown   TEXT,
    cover_url        TEXT,
    min_age          INTEGER NOT NULL DEFAULT 0,
    target_audience  TEXT NOT NULL DEFAULT 'all',
    accent_color     TEXT NOT NULL DEFAULT '#6366f1',
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK(status IN ('pending','active','banned')),
    subscriber_count INTEGER NOT NULL DEFAULT 0,
    posts_per_week   REAL NOT NULL DEFAULT 0,
    published_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Space ↔ subscriber instance bridge ────────────────────────────────────

CREATE TABLE IF NOT EXISTS space_subscribers (
    space_id    TEXT NOT NULL REFERENCES global_spaces(space_id) ON DELETE CASCADE,
    instance_id TEXT NOT NULL REFERENCES client_instances(instance_id) ON DELETE CASCADE,
    joined_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (space_id, instance_id)
);
CREATE INDEX IF NOT EXISTS idx_space_subscribers ON space_subscribers(space_id);
CREATE INDEX IF NOT EXISTS idx_instance_spaces   ON space_subscribers(instance_id);

-- ── Transport modes per client instance ───────────────────────────────────

-- Tracks the active SH↔GFS transport per client instance. The GFS leg is
-- a publicly reachable server, so it uses a persistent `wss://` WebSocket
-- (spec §24.12); `https` is the fallback for environments where the
-- WebSocket cannot stay open.
CREATE TABLE IF NOT EXISTS rtc_connections (
    instance_id   TEXT PRIMARY KEY REFERENCES client_instances(instance_id) ON DELETE CASCADE,
    transport     TEXT NOT NULL CHECK(transport IN ('websocket','https')),
    connected_at  TEXT NOT NULL DEFAULT (datetime('now')),
    last_ping_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Per-space instance bans (survives GFS_SPACE_JOIN retries) ─────────────

CREATE TABLE IF NOT EXISTS space_instance_bans (
    space_id    TEXT NOT NULL REFERENCES global_spaces(space_id) ON DELETE CASCADE,
    instance_id TEXT NOT NULL,
    banned_at   TEXT NOT NULL DEFAULT (datetime('now')),
    reason      TEXT,
    PRIMARY KEY (space_id, instance_id)
);

-- ── Fraud reports from household admins (new for GFS moderation) ──────────

CREATE TABLE IF NOT EXISTS gfs_fraud_reports (
    id                   TEXT PRIMARY KEY,
    target_type          TEXT NOT NULL CHECK(target_type IN ('space','instance')),
    target_id            TEXT NOT NULL,
    category             TEXT NOT NULL
                         CHECK(category IN ('spam','harassment',
                                            'inappropriate','misinformation',
                                            'illegal','other')),
    notes                TEXT,
    reporter_instance_id TEXT NOT NULL,
    reporter_user_id     TEXT,
    status               TEXT NOT NULL DEFAULT 'pending'
                         CHECK(status IN ('pending','dismissed','acted')),
    created_at           INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    reviewed_by          TEXT,
    reviewed_at          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_gfs_fraud_target
    ON gfs_fraud_reports(target_type, target_id, status);
CREATE INDEX IF NOT EXISTS idx_gfs_fraud_status
    ON gfs_fraud_reports(status, created_at DESC);
-- One reporter / one target — replays collapse.
CREATE UNIQUE INDEX IF NOT EXISTS idx_gfs_fraud_reporter_target
    ON gfs_fraud_reports(reporter_instance_id, target_type, target_id);

-- ── Admin portal: sessions + brute-force tracking (spec §24.9) ────────────

CREATE TABLE IF NOT EXISTS admin_sessions (
    token       TEXT PRIMARY KEY,
    expires_at  INTEGER NOT NULL,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS admin_login_attempts (
    ip           TEXT NOT NULL,
    attempted_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip
    ON admin_login_attempts(ip, attempted_at DESC);

-- ── Admin audit log (spec §24.9.10) ───────────────────────────────────────

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    action        TEXT NOT NULL,
    target_type   TEXT,
    target_id     TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    admin_ip      TEXT,
    created_at    INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_created
    ON admin_audit_log(created_at DESC);

-- ── Appeals from banned households (spec §24.9 appeal flow) ───────────────

CREATE TABLE IF NOT EXISTS gfs_appeals (
    id           TEXT PRIMARY KEY,
    target_type  TEXT NOT NULL CHECK(target_type IN ('space','instance')),
    target_id    TEXT NOT NULL,
    message      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','lifted','dismissed')),
    created_at   INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    decided_at   INTEGER,
    decided_by   TEXT
);

-- ── Public pairing tokens (spec §24.7.4) ──────────────────────────────────

CREATE TABLE IF NOT EXISTS gfs_pair_tokens (
    token        TEXT PRIMARY KEY,
    ip           TEXT NOT NULL,
    created_at   INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    consumed_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pair_tokens_ip
    ON gfs_pair_tokens(ip, created_at DESC);

-- ── Invite tokens (spec §24.8.5) ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS gfs_invite_tokens (
    gfs_token           TEXT PRIMARY KEY,
    space_id            TEXT NOT NULL REFERENCES global_spaces(space_id) ON DELETE CASCADE,
    source_instance_id  TEXT NOT NULL,
    max_uses            INTEGER NOT NULL DEFAULT 1,
    uses                INTEGER NOT NULL DEFAULT 0,
    created_at          INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    expires_at          INTEGER
);

-- ── Cluster nodes (spec §24.10) ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS cluster_nodes (
    node_id              TEXT PRIMARY KEY,
    url                  TEXT NOT NULL,
    public_key           TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL DEFAULT 'unknown'
                         CHECK(status IN ('online','offline','syncing','unknown')),
    last_seen            TEXT,
    added_at             TEXT NOT NULL DEFAULT (datetime('now')),
    active_sync_sessions INTEGER NOT NULL DEFAULT 0
);

-- ── Public highlight publications (§highlights_public) ──────────────────────────
--
-- A SH instance opts in to share a single highlight via this GFS. The row
-- holds zero highlight bytes — only enough metadata to (a) verify the
-- author signed the publish, (b) gate the public landing page on the
-- highlight's absolute ``expires_at``, and (c) give the WebRTC signalling
-- relay a key it can match against incoming offers. Highlight content
-- never transits GFS; it streams over WebRTC author → viewer.
--
-- One publication per (highlight, instance). Tokens for individual share
-- links live in ``gfs_highlight_tokens`` below.
CREATE TABLE IF NOT EXISTS gfs_highlight_publications (
    highlight_id          TEXT NOT NULL,
    instance_id       TEXT NOT NULL REFERENCES client_instances(instance_id) ON DELETE CASCADE,
    -- Unix epoch — mirrors the author's ``highlights.expires_at`` and is
    -- the absolute lifetime for every token under this publication.
    expires_at        INTEGER NOT NULL,
    published_at      INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    -- Ed25519 over the canonical publish body. Cached for audits;
    -- empty string is acceptable when the route doesn't capture the
    -- signature header (PR1 — re-verification is PR2 territory).
    publish_signature TEXT NOT NULL DEFAULT '',
    -- Filename of the cached OG-card thumbnail (under
    -- ``GfsConfig.og_thumbnail_dir``). NULL when the author opted
    -- out of social previews, or before the first upload lands.
    -- Lives on the GFS so anonymous OG crawlers (Twitter, iMessage,
    -- Slack, …) can fetch the image without needing the share token.
    og_thumbnail_filename TEXT,
    PRIMARY KEY (highlight_id, instance_id)
);
CREATE INDEX IF NOT EXISTS idx_gfs_highlight_pub_expires
    ON gfs_highlight_publications(expires_at);

-- Per-link revocable tokens. The author can mint multiple — one per
-- platform / recipient — and revoke any of them independently. Token
-- lookup is the entry point for the public landing page; ``revoked_at``
-- and the parent's ``expires_at`` together gate visibility.
CREATE TABLE IF NOT EXISTS gfs_highlight_tokens (
    token         TEXT PRIMARY KEY,
    highlight_id      TEXT NOT NULL,
    instance_id   TEXT NOT NULL,
    label         TEXT,
    created_at    INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    revoked_at    INTEGER,
    FOREIGN KEY (highlight_id, instance_id)
        REFERENCES gfs_highlight_publications(highlight_id, instance_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_gfs_highlight_tokens_pub
    ON gfs_highlight_tokens(highlight_id, instance_id);

-- ── Public Momentum directory (§Momentum-public) ────────────────────────────
--
-- A user opts in to fan their moments through this GFS. Stored per-user
-- (a single SH instance can host several authors, only some of whom
-- choose public sharing) and points back at the host instance so the
-- WS fan-out can resolve the source connection on every push.
--
-- ``user_id`` is deterministic per spec §3.2 (HMAC of home instance pk
-- + username), so ``home_instance_pk`` is also stored to let arbitrary
-- followers verify the author's per-moment signature without first
-- pairing with the host instance directly.
CREATE TABLE IF NOT EXISTS gfs_user_registrations (
    user_id            TEXT PRIMARY KEY,
    instance_id        TEXT NOT NULL REFERENCES client_instances(instance_id) ON DELETE CASCADE,
    username           TEXT NOT NULL,
    display_name       TEXT NOT NULL,
    picture_url        TEXT,
    -- Free-form bio shown on the public directory landing page. Capped
    -- at 280 characters by the register endpoint.
    bio                TEXT,
    -- Hex digest of the picture bytes uploaded to ``gfs_user_pictures``.
    -- Lets the directory list ``<img>`` tags emit a stable
    -- ``?v=<digest>`` cache buster without joining.
    picture_digest     TEXT,
    -- 64-hex Ed25519 of the author's home instance, denormalised from
    -- ``client_instances.public_key`` so the public ``/gfs/users``
    -- directory can be served without a join.
    home_instance_pk   TEXT NOT NULL,
    registered_at      INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    -- ``active`` = visible in directory + receives fan-out;
    -- ``suspended`` = admin-paused, kept for audit.
    status             TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','suspended'))
);
CREATE INDEX IF NOT EXISTS idx_gfs_user_reg_instance
    ON gfs_user_registrations(instance_id);

-- Avatar bytes mirrored onto the GFS so the public directory can
-- serve ``/gfs/users/{id}/picture`` without round-tripping to the
-- (often NAT-shielded) home instance. ``digest`` matches the
-- ``picture_digest`` column on ``gfs_user_registrations`` so the
-- upload path is idempotent on resync.
CREATE TABLE IF NOT EXISTS gfs_user_pictures (
    user_id     TEXT PRIMARY KEY REFERENCES gfs_user_registrations(user_id) ON DELETE CASCADE,
    bytes       BLOB NOT NULL,
    mime        TEXT NOT NULL,
    digest      TEXT NOT NULL,
    updated_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

-- Follower graph. ``follower_user_id`` is the calling user; the
-- ``follower_instance_id`` is the home instance their follow notice
-- arrived from (and the WS that fan-out frames push back into).
CREATE TABLE IF NOT EXISTS gfs_moment_follows (
    follower_user_id     TEXT NOT NULL,
    follower_instance_id TEXT NOT NULL REFERENCES client_instances(instance_id) ON DELETE CASCADE,
    followed_user_id     TEXT NOT NULL REFERENCES gfs_user_registrations(user_id) ON DELETE CASCADE,
    created_at           INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (follower_user_id, followed_user_id)
);
CREATE INDEX IF NOT EXISTS idx_gfs_moment_follows_followed
    ON gfs_moment_follows(followed_user_id);
CREATE INDEX IF NOT EXISTS idx_gfs_moment_follows_follower_inst
    ON gfs_moment_follows(follower_instance_id);
