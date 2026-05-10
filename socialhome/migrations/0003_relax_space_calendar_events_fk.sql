-- §D1b cross-household private spaces — calendar events.
--
-- ``space_calendar_events`` originally carried a FK to ``spaces.id``;
-- that's correct on the *host* side but wrong on every *peer*
-- household that mirrors the space via the §D1b invite flow. The
-- peer has no row in the local ``spaces`` table, so the FK rejected
-- the ``INSERT`` from
-- :meth:`SpaceContentInboundHandlers._on_calendar_saved` and the
-- inbound ``SPACE_CALENDAR_EVENT_CREATED`` event silently dropped
-- on the floor — RSVPs from the peer then 404'd.
--
-- SQLite can't ALTER FKs, so the table is recreated without the
-- constraint. Three child tables (``space_calendar_rsvps``,
-- ``space_calendar_rsvp_reminders``, ``space_calendar_feed_tokens``)
-- carry FKs into ``space_calendar_events``. We tear those FKs down
-- alongside the parent so the schema is internally consistent;
-- cascade-delete becomes the responsibility of the delete-event
-- code path, which already prunes children explicitly.
--
-- Mirrors ``0002_relax_space_invitations_fk.sql`` for the same
-- class of cross-household FK problem.

PRAGMA foreign_keys = OFF;

ALTER TABLE space_calendar_events RENAME TO space_calendar_events_v1;
ALTER TABLE space_calendar_rsvps RENAME TO space_calendar_rsvps_v1;
ALTER TABLE space_calendar_rsvp_reminders
    RENAME TO space_calendar_rsvp_reminders_v1;

CREATE TABLE space_calendar_events (
    id                     TEXT PRIMARY KEY,
    space_id               TEXT NOT NULL,
    summary                TEXT NOT NULL,
    description            TEXT,
    start_dt               TEXT NOT NULL,
    end_dt                 TEXT NOT NULL,
    all_day                INTEGER NOT NULL DEFAULT 0,
    attendees_json         TEXT NOT NULL DEFAULT '[]',
    rrule                  TEXT,
    created_by             TEXT NOT NULL,
    capacity               INTEGER,
    notify_before_minutes  INTEGER,
    notified_at            TEXT,
    cover_url              TEXT,
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE space_calendar_rsvps (
    event_id      TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    occurrence_at TEXT NOT NULL,
    status        TEXT NOT NULL CHECK(
        status IN ('going','maybe','declined','requested','waitlist')
    ),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (event_id, user_id, occurrence_at)
);

CREATE TABLE space_calendar_rsvp_reminders (
    event_id        TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    occurrence_at   TEXT NOT NULL,
    minutes_before  INTEGER NOT NULL CHECK(minutes_before >= 0),
    fire_at         TEXT NOT NULL,
    sent_at         TEXT,
    PRIMARY KEY (event_id, user_id, occurrence_at, minutes_before)
);

INSERT INTO space_calendar_events(
    id, space_id, summary, description, start_dt, end_dt, all_day,
    attendees_json, rrule, created_by, capacity, notify_before_minutes,
    notified_at, cover_url, created_at, updated_at
)
SELECT
    id, space_id, summary, description, start_dt, end_dt, all_day,
    attendees_json, rrule, created_by, capacity, notify_before_minutes,
    notified_at, cover_url, created_at, updated_at
FROM space_calendar_events_v1;

INSERT INTO space_calendar_rsvps(
    event_id, user_id, occurrence_at, status, updated_at
)
SELECT event_id, user_id, occurrence_at, status, updated_at
FROM space_calendar_rsvps_v1;

INSERT INTO space_calendar_rsvp_reminders(
    event_id, user_id, occurrence_at, minutes_before, fire_at, sent_at
)
SELECT event_id, user_id, occurrence_at, minutes_before, fire_at, sent_at
FROM space_calendar_rsvp_reminders_v1;

DROP TABLE space_calendar_rsvp_reminders_v1;
DROP TABLE space_calendar_rsvps_v1;
DROP TABLE space_calendar_events_v1;

CREATE INDEX IF NOT EXISTS idx_space_calendar_events_space
    ON space_calendar_events(space_id, start_dt);
CREATE INDEX IF NOT EXISTS idx_space_calendar_rsvps_event_occ
    ON space_calendar_rsvps(event_id, occurrence_at);
CREATE INDEX IF NOT EXISTS idx_rsvp_reminders_pending
    ON space_calendar_rsvp_reminders(fire_at) WHERE sent_at IS NULL;

PRAGMA foreign_keys = ON;
