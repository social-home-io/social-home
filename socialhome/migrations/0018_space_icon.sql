-- Space icon (avatar) image — distinct from the cover banner.
--
-- A space's identity chip / hero avatar defaults to its emoji, but admins
-- can upload an image icon for a stronger brand. Mirrors the cover blob
-- shape exactly: one WebP per space keyed by space_id, with the parent
-- ``spaces.icon_hash`` column mirroring the blob hash so the SPA can
-- cache-bust via ``/api/spaces/{id}/icon?v=<hash>``.
--
-- Migration audit (CLAUDE.md):
--   1. Audited the cover stack — the icon is a genuinely separate image
--      (distinct from cover_hash / space_covers); no existing column fits.
--   2. Alternatives rejected — overloading the cover or the emoji can't
--      represent a separate uploaded avatar.
--   3. Smallest change — one additive NULL column + one blob table,
--      mirroring space_covers; no backfill (every space starts icon-less
--      and falls back to its emoji).

ALTER TABLE spaces
    ADD COLUMN icon_hash TEXT;

CREATE TABLE IF NOT EXISTS space_icons (
    space_id     TEXT PRIMARY KEY REFERENCES spaces(id) ON DELETE CASCADE,
    bytes_webp   BLOB NOT NULL,
    hash         TEXT NOT NULL,
    width        INTEGER NOT NULL,
    height       INTEGER NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
