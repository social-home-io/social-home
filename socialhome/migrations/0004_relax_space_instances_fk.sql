-- §D1b cross-household private spaces — peer-side instance index.
--
-- ``space_instances`` originally carried a FK to ``spaces.id``; that's
-- correct on the *host* side but wrong on every *peer* household that
-- mirrors a space via the §D1b invite flow. The peer needs to know
-- "for space X the host is instance B" so its outbound RSVPs (and
-- other space-scoped events) can federate back to the right peer,
-- but it has no row in the local ``spaces`` table.
--
-- Mirrors ``0002`` and ``0003`` for the same class of problem.
-- Cascade-delete-on-space-removal becomes the responsibility of the
-- delete-space code path, which already prunes related rows
-- explicitly.

PRAGMA foreign_keys = OFF;

ALTER TABLE space_instances RENAME TO space_instances_v1;

CREATE TABLE space_instances (
    space_id      TEXT NOT NULL,
    instance_id   TEXT NOT NULL,
    joined_at     TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (space_id, instance_id)
);

INSERT INTO space_instances(space_id, instance_id, joined_at, last_seen_at)
SELECT space_id, instance_id, joined_at, last_seen_at
FROM space_instances_v1;

DROP TABLE space_instances_v1;

PRAGMA foreign_keys = ON;
