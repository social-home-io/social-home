-- 0005 — local_alias on remote_instances.
--
-- The peer's display_name comes from the federation handshake — when
-- the other side hasn't customised it (or sends something cryptic
-- like the truncated instance_id), the user is stuck staring at
-- "z7k63zfi". This column lets the local admin pick a name they want
-- to see for that connection ("Brother's house") without renaming
-- the peer remotely or federating the choice — purely local UX state.
--
-- NULL means "fall back to display_name" so the upgrade is invisible
-- for pairings that already have a friendly display_name.

ALTER TABLE remote_instances
    ADD COLUMN local_alias TEXT;
