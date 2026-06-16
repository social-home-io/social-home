-- Move-out redirect (MO-1): when a user moves out to a new household they get
-- a NEW household-scoped user_id; their OLD remote_users row gets a forwarding
-- pointer to that new identity, guarded by a monotonic move_issued_at so a
-- replayed older link can never override newer state.
--
-- Audit before a migration (CLAUDE.md):
-- 1. Audited the paths that touch this data: remote_users already keys by the
--    immutable user_id and already holds the per-user identity binding
--    (user_identity_public_key/identity_anchor, migrations 0040/0041). The
--    redirect is a property OF that existing per-user row — it is the same
--    user, now reachable at a new id — so it belongs on the row, not a new
--    table. No federation event or service cache holds this durably; the move
--    link must survive restarts so a stale-id lookup can be forwarded.
-- 2. Non-migration alternatives considered and rejected: a dedicated
--    user_moves table duplicates the (user_id -> identity) key remote_users
--    already owns and would need its own FK + cleanup on the same lifecycle;
--    computing at read time is impossible (the redirect is durable state, not
--    derivable); stuffing it into a federation event loses it across restarts.
--    The redirect is 1:1 per old row, so four columns fit the existing row.
-- 3. Smallest possible change: four additive NULL-defaulted columns, no
--    backfill, no table rewrite, no index change. Pre-move rows stay NULL
--    (== "not moved") with zero migration cost.
ALTER TABLE remote_users ADD COLUMN moved_to_user_id TEXT;
ALTER TABLE remote_users ADD COLUMN moved_to_instance_id TEXT;
ALTER TABLE remote_users ADD COLUMN move_issued_at TEXT;
ALTER TABLE remote_users ADD COLUMN move_link TEXT;
