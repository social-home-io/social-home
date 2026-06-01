-- Brand fields for a published global space, so the GFS public page renders
-- the same identity the space has on its host: a space icon (avatar) and the
-- theme's primary colour (accent already existed).
--
-- Both are additive, NULL/DEFAULT columns. ``icon_url`` holds a
-- ``data:image/webp;base64,…`` URI shipped on publish (self-contained, so the
-- public page renders it on the GFS origin without an auth-gated fetch back
-- to the host). ``primary_color`` complements the existing ``accent_color``.

ALTER TABLE global_spaces
    ADD COLUMN icon_url TEXT;

ALTER TABLE global_spaces
    ADD COLUMN primary_color TEXT NOT NULL DEFAULT '#6366f1';
