-- Space Bazaar tab + optional feed announcement.
--
-- 1. ``spaces.feature_bazaar`` — per-space toggle for the new Bazaar tab,
--    mirroring ``feature_calendar`` / ``feature_gallery``. Defaults ON so
--    every existing space surfaces the tab until an admin turns it off
--    (the SpaceFeatures dataclass field defaults to True to match).
-- 2. ``space_posts.hidden_from_feed`` — a post that exists as an entity's
--    anchor (a Bazaar listing's wrapper post, later a calendar EVENT post)
--    but should NOT appear in the feed stream. Defaults 0 so every existing
--    post stays visible; new Bazaar anchor posts set it to 1 unless the
--    seller opts to announce the listing in the feed.
--
-- Both additive, NULL-safe defaults — no backfill of existing rows beyond
-- the column default, so current behaviour is unchanged.

ALTER TABLE spaces
    ADD COLUMN feature_bazaar INTEGER NOT NULL DEFAULT 1
        CHECK(feature_bazaar IN (0, 1));

ALTER TABLE space_posts
    ADD COLUMN hidden_from_feed INTEGER NOT NULL DEFAULT 0
        CHECK(hidden_from_feed IN (0, 1));
