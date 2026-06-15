-- Phase 1 of independent user identity: per-user Ed25519 signing key family
-- (PQ-forward, mirrors instance_identity). NULL for pre-upgrade rows -> lazily
-- minted on next startup by ensure_user_identities. Private halves are
-- KEK-wrapped; only public halves ever federate. Behaviour-neutral: legacy
-- user_id stays canonical. Additive, no backfill.

ALTER TABLE users ADD COLUMN user_identity_public_key TEXT;
ALTER TABLE users ADD COLUMN user_identity_private_key TEXT;
ALTER TABLE users ADD COLUMN user_pq_algorithm TEXT;
ALTER TABLE users ADD COLUMN user_pq_public_key TEXT;
ALTER TABLE users ADD COLUMN user_pq_private_key TEXT;

ALTER TABLE remote_users ADD COLUMN user_identity_public_key TEXT;
ALTER TABLE remote_users ADD COLUMN user_pq_public_key TEXT;
