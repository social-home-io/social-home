-- §D1b cross-household invites: drop the FK from
-- ``space_invitations.space_id → spaces.id``. The same table holds rows on
-- *both* sides of a cross-household invitation:
--
-- * On the host side, ``space_id`` references a row in the local
--   ``spaces`` table — the FK was happy.
-- * On the invitee side, the space is on a *peer* instance and has no
--   row in our local ``spaces`` table. The original FK rejected the
--   ``INSERT`` from
--   :meth:`PrivateSpaceInviteHandler._on_invite`, so
--   ``GET /api/remote_invites`` always returned an empty list and
--   admins on the invitee household never saw the pending invite.
--
-- SQLite can't ALTER an existing FK, so we recreate the table without
-- the constraint. Data, indexes, and the CHECK on ``status`` are
-- preserved. Cascade-delete-on-space-removal is now the responsibility
-- of the delete-space code path, which already prunes related rows
-- explicitly.

PRAGMA foreign_keys = OFF;

ALTER TABLE space_invitations RENAME TO space_invitations_v1;

CREATE TABLE space_invitations (
    id                      TEXT PRIMARY KEY,
    space_id                TEXT NOT NULL,
    invited_user_id         TEXT NOT NULL,
    invited_by              TEXT NOT NULL,
    remote_instance_id      TEXT,
    remote_user_id          TEXT,
    invite_token            TEXT,
    space_display_hint      TEXT,
    status                  TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','accepted','declined','expired')),
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at              TEXT NOT NULL
);

INSERT INTO space_invitations(
    id, space_id, invited_user_id, invited_by,
    remote_instance_id, remote_user_id, invite_token,
    space_display_hint, status, created_at, expires_at
)
SELECT
    id, space_id, invited_user_id, invited_by,
    remote_instance_id, remote_user_id, invite_token,
    space_display_hint, status, created_at, expires_at
FROM space_invitations_v1;

DROP TABLE space_invitations_v1;

PRAGMA foreign_keys = ON;
