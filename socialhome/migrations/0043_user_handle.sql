-- Public @handle: a mutable, per-household-unique, public-facing name distinct
-- from the login `username`. Backfilled = username for existing rows; new users
-- get handle=username at provision. remote_users caches a peer's handle. Unsigned
-- display metadata (federated via USER_UPDATED), NOT identity. Per-household,
-- case-insensitive uniqueness enforced by the UNIQUE NOCASE index.
ALTER TABLE users ADD COLUMN handle TEXT;
UPDATE users SET handle = username WHERE handle IS NULL;
-- Defensive: usernames are case-SENSITIVE, so 'Bob' and 'bob' can coexist; the
-- NOCASE handle index below would otherwise abort the migration on such rows.
-- Keep the lowest-rowid row's handle bare; suffix the rest with their rowid so
-- the NOCASE-unique index can be created. (Vanishingly rare; the suffixed user
-- can pick a new handle in Settings.)
UPDATE users SET handle = handle || '_' || rowid
WHERE rowid NOT IN (SELECT MIN(rowid) FROM users GROUP BY lower(handle));
CREATE UNIQUE INDEX idx_users_handle_nocase ON users(handle COLLATE NOCASE);
ALTER TABLE remote_users ADD COLUMN handle TEXT;
