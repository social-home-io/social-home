-- Opt-in feed announcement for space calendar events (§23.15).
--
-- Space calendar events live in the Calendar tab. Historically every
-- event also auto-created a PostType.EVENT post in the feed (via
-- CalendarFeedBridge). This makes that mirror opt-in, matching the
-- Bazaar tab: ``announce_in_feed`` defaults 0 so a new event stays in
-- the Calendar tab unless the creator ticks "announce in feed".
--
-- Existing rows already have their feed post (the bridge created it at
-- the time), so the column default doesn't retroactively remove them —
-- it only governs whether *future* events mirror to the feed.
--
-- Additive, NULL-safe default — no backfill needed.

ALTER TABLE space_calendar_events
    ADD COLUMN announce_in_feed INTEGER NOT NULL DEFAULT 0
        CHECK(announce_in_feed IN (0, 1));
