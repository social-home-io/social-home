-- The ``spaces.feature_gallery`` column was reserved back in 0001 with
-- ``DEFAULT 0`` but never wired through the SpaceFeatures dataclass, so
-- existing rows all carry ``0`` while the SPA has always rendered the
-- gallery tab unconditionally. Now that admins can flip the tab on/off
-- in SpaceSettings (and SpaceFeatures.gallery defaults to True),
-- backfill the historical rows to ``1`` so existing spaces keep their
-- gallery visible until an admin explicitly turns it off.

UPDATE spaces SET feature_gallery = 1;
