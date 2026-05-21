-- Per-space feature toggles: ``feature_gallery`` was reserved back in
-- 0001 with ``DEFAULT 0`` but never wired through the SpaceFeatures
-- dataclass, and ``feature_calendar`` / ``feature_stickies`` had the
-- same ``DEFAULT 0`` even though the SPA has always rendered all three
-- tabs unconditionally. Now that admins can flip each tab on/off in
-- SpaceSettings (and the matching SpaceFeatures fields default to
-- True), backfill the historical rows so existing spaces keep every
-- tab visible until an admin explicitly turns one off.

UPDATE spaces SET feature_gallery  = 1 WHERE feature_gallery  = 0;
UPDATE spaces SET feature_calendar = 1 WHERE feature_calendar = 0;
UPDATE spaces SET feature_stickies = 1 WHERE feature_stickies = 0;
