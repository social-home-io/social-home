-- The per-space ``calendar`` and ``stickies`` toggles shipped with
-- schema defaults of ``0`` (matching the pre-existing SpaceFeatures
-- dataclass defaults), but the SPA has always rendered both tabs
-- unconditionally — so every existing space has been operating with
-- the features "on" in practice. PR #395 wired the admin toggles,
-- and this migration backfills historical rows to ``1`` so existing
-- spaces keep both tabs visible until an admin explicitly turns one
-- off. New spaces created via SpaceService get ``1`` from the
-- updated SpaceFeatures dataclass defaults.

UPDATE spaces SET feature_calendar = 1 WHERE feature_calendar = 0;
UPDATE spaces SET feature_stickies = 1 WHERE feature_stickies = 0;
