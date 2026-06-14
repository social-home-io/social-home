"""Recovery Kit service — build + restore of a household's trust layer.

A "Recovery Kit" (``.shrk`` file) lets a household reconstitute the SAME
``instance_id`` on fresh hardware: it captures the **trust layer** —
``instance_identity`` (the KEK-wrapped Ed25519 seed + routing secret),
``remote_instances`` (KEK-wrapped per-peer session keys), and ``space_keys``
(KEK-wrapped per-space content keys) — together with the ``.kek_salt`` that
the runtime KEK is derived from, all sealed behind a user passphrase by
:mod:`socialhome.services.recovery_crypto`.

This module does **whole-table dump / restore with raw SQL**. That is an
explicitly-allowed exception to the no-SQL-outside-``repositories/`` rule,
exactly like :mod:`socialhome.services.backup_service` and
:mod:`socialhome.services.data_export_service`: a snapshot/restore that copies
rows verbatim has no per-row domain logic to push into a repo.

KEK-wrapped column values are dumped and restored **verbatim** (still
wrapped). The kit also carries the ``.kek_salt`` so the restored host derives
the identical KEK and can decrypt those wrapped values — a wrapped value
without its salt is useless. Restore therefore writes ``.kek_salt`` first,
runs a **KEK self-test** (decrypt the wrapped identity seed) before touching
the database, and refuses to overwrite a populated instance — fail closed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import aiofiles
import aiofiles.os
import orjson

from ..crypto import b64url_decode, b64url_encode
from ..db import AsyncDatabase
from ..infrastructure.key_manager import KeyManager, KeyManagerError
from .recovery_crypto import seal_kit, unseal_kit

log = logging.getLogger(__name__)

#: The trust-layer tables captured by the kit, in restore order. All three
#: hold KEK-wrapped secrets; they are dumped/restored verbatim (still wrapped).
TRUST_TABLES: tuple[str, ...] = ("instance_identity", "remote_instances", "space_keys")

#: instance_config key recording the wall-clock time a restore completed.
RECOVERED_AT_KEY = "recovery.recovered_at"

_SALT_FILENAME = ".kek_salt"


class RecoveryRestoreError(RuntimeError):
    """Restore cannot proceed (instance already populated, or KEK self-test
    failed), or a kit cannot be built (no identity to back up)."""


class RecoveryKitService:
    """Build and restore passphrase-sealed trust-layer Recovery Kits."""

    __slots__ = ("_db", "_data_dir")

    def __init__(self, db: AsyncDatabase, data_dir: str | Path) -> None:
        self._db = db
        self._data_dir = Path(data_dir)

    # ── Build ──────────────────────────────────────────────────────────────

    async def build_kit(self, passphrase: str) -> bytes:
        """Dump the trust tables + ``.kek_salt``, return sealed ``.shrk`` bytes.

        Raises :class:`RecoveryRestoreError` when there is no identity to back
        up, or the ``.kek_salt`` is missing.
        """
        ident = await self._db.fetchone(
            "SELECT instance_id, created_at FROM instance_identity WHERE id='self'",
        )
        if ident is None:
            raise RecoveryRestoreError("no identity to back up")
        instance_id = ident["instance_id"]

        tables: dict[str, list[dict]] = {}
        for table in TRUST_TABLES:
            rows = await self._db.fetchall(f"SELECT * FROM {table}")
            tables[table] = [dict(r) for r in rows]

        salt = await self._read_salt()
        if salt is None:
            raise RecoveryRestoreError(
                f"{_SALT_FILENAME} missing — cannot build a recovery kit "
                "without the KEK salt",
            )

        payload = orjson.dumps(
            {"kek_salt": b64url_encode(salt), "tables": tables},
        )
        created_at_now = datetime.now(timezone.utc).isoformat()
        return seal_kit(
            payload, passphrase, instance_id=instance_id, created_at=created_at_now
        )

    # ── Restore ────────────────────────────────────────────────────────────

    async def restore_kit(self, kit_bytes: bytes, passphrase: str) -> str:
        """Reconstitute the trust layer on a fresh instance.

        Returns the restored ``instance_id``. Raises
        :class:`RecoveryRestoreError` if an identity already exists or the KEK
        self-test fails; propagates ``RecoveryKitError`` /
        ``UnsupportedRecoverySuite`` from the codec (wrong passphrase, tamper,
        unknown suite). Fails closed: ``.kek_salt`` is written and the KEK is
        self-tested **before** any row is inserted.
        """
        existing = await self._db.fetchone(
            "SELECT 1 FROM instance_identity WHERE id='self'",
        )
        if existing is not None:
            raise RecoveryRestoreError(
                "instance already has an identity; restore only into an empty instance",
            )

        header, payload = unseal_kit(kit_bytes, passphrase)
        data = orjson.loads(payload)
        salt = b64url_decode(data["kek_salt"])
        if len(salt) != KeyManager.KEK_BYTES:
            raise RecoveryRestoreError(
                f"recovery kit .kek_salt has unexpected length {len(salt)}",
            )
        tables: dict[str, list[dict]] = data["tables"]

        # Write the salt FIRST (overwriting any startup-minted random salt) so
        # KeyManager.from_data_dir derives the kit's KEK, then self-test.
        await self._write_salt(salt)
        self_row = self._find_self_row(tables)
        self._kek_self_test(self_row)

        await self._insert_trust_tables(tables)

        await self._db.enqueue(
            "INSERT INTO instance_config(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (RECOVERED_AT_KEY, datetime.now(timezone.utc).isoformat()),
        )

        if self_row is not None and self_row.get("instance_id"):
            return str(self_row["instance_id"])
        return str(header["instance_id"])

    # ── Helpers ──────────────────────────────────────────────────────────--

    @staticmethod
    def _find_self_row(tables: dict[str, list[dict]]) -> dict | None:
        for row in tables.get("instance_identity", []):
            if row.get("id") == "self":
                return row
        return None

    def _kek_self_test(self, self_row: dict | None) -> None:
        """Prove the runtime KEK (from the just-written salt) decrypts the
        wrapped identity seed. Run BEFORE inserting any row so a salt/KEK
        mismatch never leaves a half-restored, undecryptable instance."""
        if self_row is None:
            raise RecoveryRestoreError(
                "recovery kit has no instance_identity self row",
            )
        km = KeyManager.from_data_dir(self._data_dir)
        wrapped = self_row.get("identity_private_key")
        try:
            seed = km.decrypt(str(wrapped))
        except KeyManagerError as exc:
            raise RecoveryRestoreError(
                "KEK self-test failed — wrong .kek_salt / KEK passphrase mode mismatch",
            ) from exc
        if len(seed) != 32:
            raise RecoveryRestoreError(
                "KEK self-test failed — wrong .kek_salt / KEK passphrase mode mismatch",
            )

    async def _insert_trust_tables(self, tables: dict[str, list[dict]]) -> None:
        """Insert all trust rows in one atomic transaction.

        FK enforcement is suspended for the insert: ``space_keys`` references
        ``spaces(id)``, but ``spaces`` is NOT part of the trust layer (it
        re-syncs over federation), so the rows are legitimately orphaned at
        restore time. The pragma is restored in a ``finally`` so a later
        request still gets referential integrity.
        """

        def _run(conn: sqlite3.Connection) -> None:
            # transact() has already opened BEGIN IMMEDIATE. PRAGMA
            # foreign_keys is a no-op inside a transaction, so close it,
            # run the FK-suspended insert in its own transaction, then
            # re-open one for transact()'s trailing COMMIT.
            conn.execute("COMMIT")
            conn.execute("PRAGMA foreign_keys=OFF")
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for table in TRUST_TABLES:
                        for row in tables.get(table, []):
                            self._insert_row(conn, table, row)
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN IMMEDIATE")

        await self._db.transact(_run)

    @staticmethod
    def _insert_row(conn: sqlite3.Connection, table: str, row: dict) -> None:
        if not row:
            return
        cols = list(row.keys())
        col_list = ", ".join(cols)
        placeholders = ", ".join("?" for _ in cols)
        sql = f"INSERT OR IGNORE INTO {table}({col_list}) VALUES({placeholders})"
        conn.execute(sql, tuple(row[c] for c in cols))

    async def _read_salt(self) -> bytes | None:
        path = self._data_dir / _SALT_FILENAME
        if not await aiofiles.os.path.isfile(path):
            return None
        async with aiofiles.open(path, "rb") as f:
            return await f.read()

    async def _write_salt(self, salt: bytes) -> None:
        await aiofiles.os.makedirs(self._data_dir, exist_ok=True)
        path = self._data_dir / _SALT_FILENAME
        async with aiofiles.open(path, "wb") as f:
            await f.write(salt)
        # aiofiles.os has no chmod; run the sync call off the event loop.
        await asyncio.to_thread(os.chmod, path, 0o600)
