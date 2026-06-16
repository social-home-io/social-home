-- UUID identity anchor: user_id derives from this immutable value instead of
-- the username. Existing rows backfill to their current username so their
-- user_id is unchanged; new users get a uuid4 at provision. remote_users caches
-- the peer user's anchor. Additive; backfill existing rows only.
ALTER TABLE users ADD COLUMN identity_anchor TEXT;
UPDATE users SET identity_anchor = username WHERE identity_anchor IS NULL;
ALTER TABLE remote_users ADD COLUMN identity_anchor TEXT;
