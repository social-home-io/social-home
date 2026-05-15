-- 0004_calendar_client_event_uuid.sql
--
-- Stamp a client-minted UUID on every row of a multi-attendee
-- ``calendar_events`` fan-out so the agenda can group the rows back
-- into a single card by intent — not by the brittle content
-- heuristic that PR #326 shipped as a stopgap (see issue #327 for
-- the full design rationale).
--
-- The composer-side rule is: mint one v4 UUID on the SPA before the
-- multi-target ``POST /api/calendars/{id}/events`` batch, then send
-- it verbatim on every request in the batch. Every resulting row
-- carries the same ``client_event_uuid``. ``groupSharedEvents``
-- prefers this when both rows carry it; falls back to the content
-- key for legacy / externally-imported rows.
--
-- Nullable on purpose:
--
--   * existing rows from before this migration keep working via the
--     content-key fallback (they stay ``NULL``).
--   * federation envelopes from sub-version peers don't carry the
--     field; the receiver persists ``NULL`` and the content key
--     covers the legacy case.
--   * imports from external ICS / CalDAV land without a UUID.
--
-- No proto-version bump is needed because the field is purely
-- additive on the wire: receivers that ignore unknown payload keys
-- just persist ``NULL`` and lose the UUID-keyed grouping (the
-- content heuristic still merges those rows on the SPA side).

ALTER TABLE calendar_events
  ADD COLUMN client_event_uuid TEXT;

CREATE INDEX IF NOT EXISTS idx_calendar_events_client_event_uuid
  ON calendar_events(client_event_uuid)
  WHERE client_event_uuid IS NOT NULL;
