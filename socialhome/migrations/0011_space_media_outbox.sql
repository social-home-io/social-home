-- Per-peer outbox for space-post media bytes (§D1b federation).
--
-- Audit per the CLAUDE.md "Before adding a SQL migration" rule:
--
-- 1. Existing code paths touching this data: ``dm_media_outbox``
--    (DM media bytes federation, same shape). Tried sharing the
--    same table — message_id FK is to ``conversation_messages``;
--    space post bytes need ``space_posts(id)`` instead. The two
--    workloads ALSO have separate schedulers and separate
--    backoff curves so giving them one table would force them to
--    share rate-limiting / quota / failure-handling logic. Cleaner
--    to ship a sibling table that mirrors the shape but FKs to
--    ``space_posts``.
-- 2. Non-migration alternative considered — could we ship the
--    bytes inline on ``SPACE_POST_CREATED`` and skip the outbox
--    entirely? Tried this in v1; rejected because:
--      a) federation envelope size cap (~25 MiB after signing +
--         base64) means a 1080p video can't ship inline at all;
--      b) sending bytes synchronously during fan-out holds
--         multi-MB buffers in scope per-peer per-blob;
--      c) crash recovery is implicit: if the server dies mid
--         fan-out, peers that didn't receive the blob have NO
--         retry path.
--    The outbox + scheduler pattern (which DM already proved
--    out) handles all three: file is read lazily per attempt,
--    chunks are bounded at 256 KiB, retry is automatic with
--    exponential backoff.
-- 3. Minimality — additive new table. Mirrors
--    ``dm_media_outbox`` field-for-field except the FK target.
--    ``post_id`` references ``space_posts(id)`` so a post delete
--    auto-purges the outbox rows.
CREATE TABLE IF NOT EXISTS space_media_outbox (
    blob_id              TEXT NOT NULL,
    post_id              TEXT NOT NULL REFERENCES space_posts(id) ON DELETE CASCADE,
    target_instance_id   TEXT NOT NULL,
    -- Path under the local media root holding the bytes. The
    -- scheduler reads from here lazily on each attempt and ships
    -- in chunks if the file exceeds ``SINGLE_CHUNK_BYTES_THRESHOLD``.
    bytes_path           TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'in_flight', 'failed')),
    attempts             INTEGER NOT NULL DEFAULT 0,
    next_attempt_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_error           TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (blob_id, target_instance_id)
);
CREATE INDEX IF NOT EXISTS idx_space_media_outbox_due
    ON space_media_outbox(status, next_attempt_at)
    WHERE status = 'pending';
