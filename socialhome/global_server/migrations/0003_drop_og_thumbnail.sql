-- Drop the highlight OG-thumbnail cache. The feature let an author upload a
-- JPEG social-card preview that the GFS stored on disk and served to anonymous
-- OG crawlers. It has been removed entirely so the GFS stores ZERO content
-- bytes — only the signed routing metadata for a published highlight. The
-- highlight itself always streamed author → viewer over WebRTC; the cached
-- preview was the lone exception, now gone.
--
-- Destructive but minimal: a single column drop on a table that otherwise
-- keeps its shape. SQLite >= 3.35 supports ALTER TABLE ... DROP COLUMN.

ALTER TABLE gfs_highlight_publications DROP COLUMN og_thumbnail_filename;
