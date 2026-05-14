-- 0002_calendar_timezone.sql
--
-- Anchor every calendar event to an IANA timezone so wall-clock
-- semantics survive DST transitions and a user travelling. UTC stays
-- the storage shape on disk (matches the CLAUDE.md "DB stores UTC,
-- SPA converts at boundary" rule); the ``tz`` columns added here carry
-- the *wall-clock anchor* — e.g. "every Tuesday 19:00 Europe/Berlin"
-- resolves correctly across spring-forward / fall-back instead of
-- drifting by ±1 h.
--
-- Resolution chain at event-creation time:
--   event.tz ?? space.tz ?? user.tz ?? household_features.tz
--
-- All columns added here are nullable / have safe defaults; existing
-- rows from before this fix shipped keep their (slightly wrong) stored
-- timestamps. The project is early enough that re-creating an off-by-
-- offset birthday is cheaper than a data migration.

-- Every tz column is ``NOT NULL DEFAULT 'UTC'``:
--   * ADD COLUMN backfills pre-existing rows with ``'UTC'``.
--   * Every read returns a concrete IANA name (no NULL handling in the
--     repo / service / SPA — the column itself is the contract).
--   * Domain dataclasses can use ``tz: str = "UTC"`` instead of
--     ``str | None``, so route handlers, federation payloads and the
--     SPA never have to think about an "inherit" state at runtime.
--
-- Resolution still happens at *create* time in the service layer: a
-- POST without an explicit tz inherits from the parent (space.tz for
-- space events, household.tz for personal events). After the row is
-- written, the column carries the concrete value forever — admins
-- editing the space / household tz later do not retroactively move
-- existing events.

ALTER TABLE household_features
    ADD COLUMN tz TEXT NOT NULL DEFAULT 'UTC';

ALTER TABLE users
    ADD COLUMN tz TEXT NOT NULL DEFAULT 'UTC';

ALTER TABLE spaces
    ADD COLUMN tz TEXT NOT NULL DEFAULT 'UTC';

ALTER TABLE calendar_events
    ADD COLUMN tz TEXT NOT NULL DEFAULT 'UTC';

ALTER TABLE space_calendar_events
    ADD COLUMN tz TEXT NOT NULL DEFAULT 'UTC';
