-- Cross-household location pins on the space map (§D1b).
--
-- Audit per the CLAUDE.md "Before adding a SQL migration" rule:
--
-- 1. Existing code paths touching this data: ``presence`` (local
--    household HA-derived presence) and ``remote_presence``
--    (general per-peer presence from PRESENCE_UPDATED events).
--    Neither is space-scoped — the per-space rendering needs both
--    privacy-tier awareness (the host might render GPS in one
--    space and zone-only in another) AND space membership filter
--    (a user opted into space A but not space B). Trying to
--    overload ``remote_presence`` would smear those decisions
--    across a row that today represents "what the SPA shows on
--    the home map".
-- 2. Non-migration alternative considered — could the SPA
--    re-query the remote peer for each map render? No: privacy
--    tier choices (zone_only vs gps) happen sender-side, the
--    receiver must store what it was told; a pull model would
--    also re-query at every map refresh, which is expensive on
--    mobile. Could we put the latest pin on the
--    ``space_remote_members`` row? Tempting but conflates two
--    concerns: the member-roster row should be cold metadata
--    (display_name, role), the location is volatile and changes
--    every few seconds; mixing them invalidates the row cache
--    every location update.
-- 3. Minimality — additive new table. ``REFERENCES spaces(id)``
--    so a SPACE_DISSOLVED cascade cleans the pins automatically.
--    No FK on ``instance_id`` / ``user_id`` because
--    ``remote_users`` / ``remote_instances`` may legitimately
--    arrive after the first location update (the location packet
--    races the membership packet through the outbox).
CREATE TABLE IF NOT EXISTS space_remote_member_locations (
    space_id     TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    instance_id  TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    -- Privacy tier the sender chose. ``gps`` carries lat/lon +
    -- accuracy_m; ``zone_only`` carries zone_id + zone_name and
    -- leaves coordinates NULL.
    mode         TEXT NOT NULL CHECK(mode IN ('gps', 'zone_only')),
    latitude     REAL,           -- 4dp-truncated at sender; NULL in zone_only mode
    longitude    REAL,
    accuracy_m   REAL,
    zone_id      TEXT,           -- The host's space_zones.id (zone_only mode)
    zone_name    TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (space_id, instance_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_space_remote_member_locations_space
    ON space_remote_member_locations(space_id);
