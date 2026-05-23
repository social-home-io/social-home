-- Generalize ``space_media_outbox`` so gallery items can use it too.
--
-- Audit per the CLAUDE.md "Before adding a SQL migration" rule:
--
-- 1. Existing code paths touching this data: the brand-new (PR #440)
--    space-post media sync. The original table FK-ed ``post_id`` to
--    ``space_posts(id)`` so a post delete auto-cascaded the rows.
--    Pascal asked: does the same federation pattern work for
--    galleries? Yes if we loosen the FK so the same outbox can
--    carry a gallery item's bytes alongside a post's.
-- 2. Non-migration alternative considered — could we add a second
--    sibling table ``space_gallery_media_outbox`` instead?
--    Rejected: the chunked-dispatch / backoff / reclaim machinery
--    is identical; doubling the table only doubles the scheduler
--    plumbing without separating any actual concern. The DM /
--    space split exists because the schedulers backoff
--    independently (a stuck DM peer mustn't starve space sends);
--    splitting "space post" vs "space gallery" buys nothing.
-- 3. Minimality — SQLite can't drop a FK in place. Standard
--    table-rebuild dance: rename → create new shape → copy → drop
--    old → rename new. Pending rows are dropped on the rebuild
--    because (a) the table just landed in PR #440 so the
--    on-disk row count for any deployed instance is effectively
--    zero, and (b) the scheduler self-heals: a missed blob lives
--    on the sender's media path, the next post / gallery upload
--    re-enqueues, the receiver's §25.6 catch-up sync covers the
--    gap. No backfill needed.
-- Drop the index before the rename so the CREATE INDEX below
-- doesn't trip "already exists" on the rebuilt table.
DROP INDEX IF EXISTS idx_space_media_outbox_due;

ALTER TABLE space_media_outbox RENAME TO _space_media_outbox_old;

CREATE TABLE space_media_outbox (
    blob_id              TEXT NOT NULL,
    -- ``space_id`` is the hard scope — a SPACE_DISSOLVED cascades
    -- the rows automatically. ``correlation_id`` is the soft
    -- backref (a post_id, a gallery_item_id, etc.); the scheduler
    -- never reads it (file lives at ``bytes_path``), but the SPA's
    -- "where did this blob come from" debug surface uses it.
    space_id             TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    correlation_id       TEXT NOT NULL,
    target_instance_id   TEXT NOT NULL,
    bytes_path           TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'in_flight', 'failed')),
    attempts             INTEGER NOT NULL DEFAULT 0,
    next_attempt_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_error           TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (blob_id, target_instance_id)
);
CREATE INDEX idx_space_media_outbox_due
    ON space_media_outbox(status, next_attempt_at)
    WHERE status = 'pending';

DROP TABLE _space_media_outbox_old;
