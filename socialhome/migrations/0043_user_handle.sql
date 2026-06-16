-- Public @handle: a mutable, per-household-unique, public-facing name distinct
-- from the login `username`. Backfilled = username for existing rows; new users
-- get handle=username at provision. remote_users caches a peer's handle. Unsigned
-- display metadata (federated via USER_UPDATED), NOT identity. Per-household,
-- case-insensitive uniqueness enforced by the UNIQUE NOCASE index.
ALTER TABLE users ADD COLUMN handle TEXT;
UPDATE users SET handle = username WHERE handle IS NULL;
CREATE UNIQUE INDEX idx_users_handle_nocase ON users(handle COLLATE NOCASE);
ALTER TABLE remote_users ADD COLUMN handle TEXT;
