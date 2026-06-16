-- 0042 — Make username a mutable label: ON UPDATE CASCADE on every
-- users(username) / platform_users(username) foreign key.
--
-- Why a rebuild (not an ALTER): SQLite cannot alter a foreign-key clause in
-- place — adding ``ON UPDATE CASCADE`` to an existing FK requires the
-- documented "create new table → copy → drop → rename" dance
-- (https://sqlite.org/lang_altertable.html, §"Making Other Kinds Of Table
-- Schema Changes"). Each table below is rebuilt EXACTLY as it currently
-- exists (its 0001 shape — none gained later columns via ALTER), changing
-- ONLY the FK clause to append ``ON UPDATE CASCADE`` (the pre-existing
-- ``ON DELETE CASCADE`` is preserved), and every index it carried is
-- recreated (DROP TABLE drops a table's indexes with it).
--
-- CLAUDE.md "audit before a migration":
--   * Audited every code path: ``username`` is the parent key for these six
--     child tables. Today a rename is impossible because the FKs lack
--     ON UPDATE — a rename would orphan or reject the child rows. The
--     minimal correct propagation is ON UPDATE CASCADE on each FK.
--   * Alternative considered & rejected: re-point every FK at the immutable
--     ``users.user_id`` / ``identity_anchor`` instead of ``username``. That
--     is a far heavier change (new columns, backfill, every read/write path
--     rewritten) for the same outcome; ON UPDATE CASCADE keeps the existing
--     keying and is purely additive in spirit — no row is dropped, data is
--     preserved by an explicit-column copy.
--   * Smallest possible change: only the FK action changes; column sets,
--     types, defaults, CHECKs, PKs and indexes are reproduced verbatim.
--
-- ``calendars`` is referenced by ``calendar_events(calendar_id)``, so the
-- rebuild runs with ``foreign_keys`` momentarily OFF — the SQLite-mandated
-- procedure for rebuilding a *parent* table — then re-enables enforcement.
-- Safety comes from the faithful, id-preserving row copy (no reference is
-- broken). ``foreign_key_check`` below is informational only: under
-- ``executescript`` it returns any violation rows rather than raising, and the
-- runner discards results, so it does not abort the migration — it is a
-- developer tripwire when run by hand, not an enforced guard.
-- The runner opens the connection in autocommit mode, so these PRAGMAs take
-- effect between statements.
PRAGMA foreign_keys=OFF;

-- ── presence (users.username PK + FK) ───────────────────────────────────────
CREATE TABLE presence_new (
    username           TEXT PRIMARY KEY REFERENCES users(username) ON DELETE CASCADE ON UPDATE CASCADE,
    entity_id          TEXT NOT NULL,
    state              TEXT NOT NULL DEFAULT 'unavailable'
                       CHECK(state IN ('home','zone','away','unavailable')),
    zone_name          TEXT,
    latitude           REAL,
    longitude          REAL,
    gps_accuracy_m     REAL,
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO presence_new
    (username, entity_id, state, zone_name, latitude, longitude,
     gps_accuracy_m, updated_at)
SELECT
    username, entity_id, state, zone_name, latitude, longitude,
    gps_accuracy_m, updated_at
FROM presence;
DROP TABLE presence;
ALTER TABLE presence_new RENAME TO presence;

-- ── post_drafts (users.username FK) ─────────────────────────────────────────
CREATE TABLE post_drafts_new (
    id           TEXT PRIMARY KEY,
    username     TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE ON UPDATE CASCADE,
    context      TEXT NOT NULL,
    type         TEXT NOT NULL DEFAULT 'text',
    content      TEXT NOT NULL DEFAULT '',
    media_url    TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO post_drafts_new
    (id, username, context, type, content, media_url, updated_at)
SELECT
    id, username, context, type, content, media_url, updated_at
FROM post_drafts;
DROP TABLE post_drafts;
ALTER TABLE post_drafts_new RENAME TO post_drafts;
CREATE INDEX IF NOT EXISTS idx_post_drafts_user_ctx
    ON post_drafts(username, context);

-- ── space_aliases (users.username FK) ───────────────────────────────────────
CREATE TABLE space_aliases_new (
    space_id        TEXT NOT NULL,
    local_username  TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE ON UPDATE CASCADE,
    alias           TEXT NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (space_id, local_username)
);
INSERT INTO space_aliases_new
    (space_id, local_username, alias, updated_at)
SELECT
    space_id, local_username, alias, updated_at
FROM space_aliases;
DROP TABLE space_aliases;
ALTER TABLE space_aliases_new RENAME TO space_aliases;
CREATE INDEX IF NOT EXISTS idx_space_aliases_local_username
    ON space_aliases(local_username);

-- ── conversation_members (users.username FK) ────────────────────────────────
CREATE TABLE conversation_members_new (
    conversation_id       TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    username              TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE ON UPDATE CASCADE,
    joined_at             TEXT NOT NULL DEFAULT (datetime('now')),
    last_read_at          TEXT,
    history_visible_from  TEXT,
    deleted_at            TEXT,
    PRIMARY KEY (conversation_id, username)
);
INSERT INTO conversation_members_new
    (conversation_id, username, joined_at, last_read_at,
     history_visible_from, deleted_at)
SELECT
    conversation_id, username, joined_at, last_read_at,
    history_visible_from, deleted_at
FROM conversation_members;
DROP TABLE conversation_members;
ALTER TABLE conversation_members_new RENAME TO conversation_members;
CREATE INDEX IF NOT EXISTS idx_conversation_members_username
    ON conversation_members(username);

-- ── calendars (users.username FK; referenced by calendar_events) ────────────
CREATE TABLE calendars_new (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    color           TEXT NOT NULL DEFAULT '#4A90E2',
    owner_username  TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE ON UPDATE CASCADE,
    calendar_type   TEXT NOT NULL DEFAULT 'personal'
                    CHECK(calendar_type IN ('personal','space'))
);
INSERT INTO calendars_new
    (id, name, color, owner_username, calendar_type)
SELECT
    id, name, color, owner_username, calendar_type
FROM calendars;
DROP TABLE calendars;
ALTER TABLE calendars_new RENAME TO calendars;
CREATE INDEX IF NOT EXISTS idx_calendars_owner ON calendars(owner_username);

-- ── platform_tokens (platform_users.username FK) ────────────────────────────
CREATE TABLE platform_tokens_new (
    token_id      TEXT PRIMARY KEY,
    username      TEXT NOT NULL REFERENCES platform_users(username) ON DELETE CASCADE ON UPDATE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at    TEXT,
    last_used_at  TEXT
);
INSERT INTO platform_tokens_new
    (token_id, username, token_hash, created_at, expires_at, last_used_at)
SELECT
    token_id, username, token_hash, created_at, expires_at, last_used_at
FROM platform_tokens;
DROP TABLE platform_tokens;
ALTER TABLE platform_tokens_new RENAME TO platform_tokens;
CREATE INDEX IF NOT EXISTS idx_platform_tokens_username
    ON platform_tokens(username);

-- Re-enable enforcement and verify the rebuild introduced no dangling FK.
PRAGMA foreign_key_check;
PRAGMA foreign_keys=ON;
