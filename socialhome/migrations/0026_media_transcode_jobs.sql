-- 0026_media_transcode_jobs.sql
--
-- Async video transcoding outbox (background-job conversion of the
-- previously-synchronous upload-time transcode). One row per uploaded
-- video, keyed by the eventual output filename (the canonical media
-- key the rest of the app stores and serves).
--
-- Migration audit (mandatory 3-point):
--
--   1. Audited every code path that touches this data. No generic
--      media-transcode / job-queue table exists. ``dm_media_outbox``
--      (0003) is DM-federation-specific — one row per remote delivery
--      target, holding a path to *already-transcoded* bytes for a
--      ``DM_MEDIA_BLOB`` send — not a local transcode queue.
--      ``gallery_items`` / ``feed_posts`` carry no media-status column.
--   2. Alternative rejected: a per-table ``media_status`` column would
--      multiply across feed / gallery / momentum / DM / highlights and
--      need backfilling on each. A single queue keyed by the output
--      filename is one source of truth; readiness is derived at read
--      time (absent row = ready), and rows are transient — deleted on
--      completion — so the table stays small.
--   3. Smallest possible change: additive ``CREATE TABLE`` (+ one
--      index) only. No existing row is touched.

CREATE TABLE IF NOT EXISTS media_transcode_jobs (
    output_filename     TEXT PRIMARY KEY,        -- eventual transcoded ".webm" UUID filename (canonical media key)
    source_path         TEXT NOT NULL,           -- temp path of the uploaded source bytes on disk
    thumbnail_filename  TEXT NOT NULL,           -- ".webp" poster UUID filename produced alongside
    kind                TEXT NOT NULL DEFAULT 'video' CHECK(kind IN ('video')),
    owner_user_id       TEXT,                    -- uploader (for targeting the media.ready WS frame); nullable
    status              TEXT NOT NULL DEFAULT 'pending'
                          CHECK(status IN ('pending', 'processing', 'failed')),
    attempts            INTEGER NOT NULL DEFAULT 0,
    next_attempt_at     TEXT NOT NULL DEFAULT (datetime('now')),
    last_error          TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_media_transcode_due
    ON media_transcode_jobs(status, next_attempt_at);
