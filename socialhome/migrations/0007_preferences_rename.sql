-- household_features → preferences. The singleton CHECK is replaced by
-- a polymorphic id ('household' or <user_id>) so per-user preference
-- rows can coexist with the household-wide row. feat_highlights and
-- feat_momentum are dropped (Highlights and Momentum are per-person
-- content surfaces, not household-wide). hide_highlights /
-- hide_momentum / hide_bazaar replace the household-level gate at
-- user scope. feat_presence and feat_gallery are added for symmetry.

ALTER TABLE household_features RENAME TO _household_features_old;

CREATE TABLE preferences (
    id                    TEXT PRIMARY KEY,
    household_name        TEXT NOT NULL DEFAULT 'Home',
    tz                    TEXT NOT NULL DEFAULT 'UTC',
    feat_feed             INTEGER NOT NULL DEFAULT 1 CHECK(feat_feed IN (0, 1)),
    feat_pages            INTEGER NOT NULL DEFAULT 1 CHECK(feat_pages IN (0, 1)),
    feat_tasks            INTEGER NOT NULL DEFAULT 1 CHECK(feat_tasks IN (0, 1)),
    feat_stickies         INTEGER NOT NULL DEFAULT 1 CHECK(feat_stickies IN (0, 1)),
    feat_calendar         INTEGER NOT NULL DEFAULT 1 CHECK(feat_calendar IN (0, 1)),
    feat_presence         INTEGER NOT NULL DEFAULT 1 CHECK(feat_presence IN (0, 1)),
    feat_gallery          INTEGER NOT NULL DEFAULT 1 CHECK(feat_gallery IN (0, 1)),
    allow_text            INTEGER NOT NULL DEFAULT 1 CHECK(allow_text IN (0, 1)),
    allow_image           INTEGER NOT NULL DEFAULT 1 CHECK(allow_image IN (0, 1)),
    allow_video           INTEGER NOT NULL DEFAULT 1 CHECK(allow_video IN (0, 1)),
    allow_file            INTEGER NOT NULL DEFAULT 1 CHECK(allow_file IN (0, 1)),
    allow_poll            INTEGER NOT NULL DEFAULT 1 CHECK(allow_poll IN (0, 1)),
    allow_schedule        INTEGER NOT NULL DEFAULT 1 CHECK(allow_schedule IN (0, 1)),
    allow_location        INTEGER NOT NULL DEFAULT 1 CHECK(allow_location IN (0, 1)),
    allow_highlight_share INTEGER NOT NULL DEFAULT 1 CHECK(allow_highlight_share IN (0, 1)),
    hide_highlights       INTEGER NOT NULL DEFAULT 0 CHECK(hide_highlights IN (0, 1)),
    hide_momentum         INTEGER NOT NULL DEFAULT 0 CHECK(hide_momentum IN (0, 1)),
    hide_bazaar           INTEGER NOT NULL DEFAULT 0 CHECK(hide_bazaar IN (0, 1))
);

INSERT INTO preferences (
    id, household_name, tz,
    feat_feed, feat_pages, feat_tasks, feat_stickies, feat_calendar,
    allow_text, allow_image, allow_video, allow_file, allow_poll,
    allow_schedule, allow_location, allow_highlight_share
)
SELECT
    'household', household_name, tz,
    feat_feed, feat_pages, feat_tasks, feat_stickies, feat_calendar,
    allow_text, allow_image, allow_video, allow_file, allow_poll,
    allow_schedule, allow_location, allow_highlight_share
FROM _household_features_old;

DROP TABLE _household_features_old;
