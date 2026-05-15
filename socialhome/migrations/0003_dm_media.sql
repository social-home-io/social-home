-- 0003_dm_media.sql
--
-- Picture / video / file attachments in DMs (§5.2 expansion).
--
-- Two-tier scope:
--
--   1. New ``"file"`` value in ``MESSAGE_TYPES`` (image / video already
--      shipped in v1 of the schema — what was missing was the metadata
--      a generic file needs to render without a content sniff).
--   2. Cross-household preview-now-sync-later: the sender embeds a
--      tiny preview inside the encrypted ``DM_MESSAGE`` envelope so
--      the receiver renders something *immediately*; the full-quality
--      bytes ride a follow-up ``DM_MEDIA_BLOB`` event delivered by
--      the new outbox below. ``media_url`` on the receiver flips from
--      the local-preview path to the local-full-bytes path once the
--      blob lands and a ``dm.media_ready`` WS frame fires.
--
-- All columns added here are nullable. Existing rows from before this
-- migration shipped were ``type='text'`` with no media, so the new
-- columns staying ``NULL`` is the natural backfill.

-- ── conversation_messages: file metadata + cross-household sync tags ──

-- File-name as the sender labelled the upload. Optional even on
-- ``type='file'`` rows so deeply unusual uploads (e.g. a paste of raw
-- clipboard bytes) still pass through; the SPA falls back to the MIME
-- glyph + "Attached file".
ALTER TABLE conversation_messages
    ADD COLUMN file_name TEXT;

-- IANA media type (``image/webp``, ``video/webm``, ``application/pdf``,
-- ``text/plain``, ...). Drives the bubble's render branch on the
-- receiver — image / video / generic-file pill.
ALTER TABLE conversation_messages
    ADD COLUMN mime_type TEXT;

-- Authoritative byte count of the full media (post-transcoding for
-- image / video, raw for file). Surfaces in the bubble as
-- "1.2 MB" so the recipient knows what they're about to load on a
-- metered connection. NULL on legacy rows.
ALTER TABLE conversation_messages
    ADD COLUMN file_size_bytes INTEGER;

-- Stable identifier shared across the ``DM_MESSAGE`` envelope and the
-- follow-up ``DM_MEDIA_BLOB`` event. The receiver uses it to
-- correlate "this is the full version of the preview I already have"
-- and to upsert the same ``conversation_messages.id`` row. NULL when
-- the message has no media OR the conversation is same-household
-- (no federation round-trip needed).
ALTER TABLE conversation_messages
    ADD COLUMN media_blob_id TEXT;

-- Receiver-side sync state for cross-household media. NULL when the
-- message is local OR the full-quality file has arrived and
-- ``media_url`` now points at the local full bytes. ``'pending'``
-- means the bubble currently renders the preview embedded in the
-- envelope and is waiting for the matching ``DM_MEDIA_BLOB``;
-- ``'failed'`` records that the sender gave up after the outbox
-- retry policy expired. The receiver SPA reads this column off
-- ``GET /api/conversations/{id}/messages`` to decide whether to show
-- a spinner overlay on the bubble.
ALTER TABLE conversation_messages
    ADD COLUMN media_sync_status TEXT
        CHECK (media_sync_status IN ('pending', 'failed') OR media_sync_status IS NULL);

-- ── dm_media_outbox: full-bytes-pending federation queue ─────────────
--
-- One row per (message_id × remote_instance_id) that still owes a
-- ``DM_MEDIA_BLOB`` send. Rows are inserted by ``DmService`` after a
-- successful ``DM_MESSAGE`` outbound and consumed by the scheduler in
-- ``DmMediaSyncService`` (lands in a follow-up PR; the table is added
-- here so the schema migration is a single atomic change).
--
-- Retries follow the same exponential-backoff shape as the main
-- federation outbox — see ``ResilientFederationOutbox`` for the
-- canonical pattern. Once ``attempts`` hits the configured cap the
-- row flips to ``failed`` and the SPA shows a "media couldn't be
-- delivered to <peer>" footnote on the sender's own bubble.
CREATE TABLE IF NOT EXISTS dm_media_outbox (
    blob_id              TEXT NOT NULL,
    message_id           TEXT NOT NULL REFERENCES conversation_messages(id) ON DELETE CASCADE,
    target_instance_id   TEXT NOT NULL,
    -- Path under the local media root holding the full-quality
    -- transcoded bytes. The scheduler reads from here, encrypts
    -- under the conversation key, and ships in chunks if needed.
    bytes_path           TEXT NOT NULL,
    -- ``pending`` = waiting for next attempt; ``in_flight`` = the
    -- scheduler holds a lease; ``done`` = peer confirmed receipt
    -- (row is deleted after confirmation, ``done`` is the brief
    -- in-memory state before delete); ``failed`` = exceeded retry
    -- cap.
    status               TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'in_flight', 'failed')),
    attempts             INTEGER NOT NULL DEFAULT 0,
    next_attempt_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_error           TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (blob_id, target_instance_id)
);
CREATE INDEX IF NOT EXISTS idx_dm_media_outbox_due
    ON dm_media_outbox(status, next_attempt_at)
    WHERE status = 'pending';
