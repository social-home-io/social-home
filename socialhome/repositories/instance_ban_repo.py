"""Household-level instance bans (§Momentum-relay-policy).

A household admin can mark a remote instance as *banned* on this
instance. Inbound envelopes from a banned instance are dropped at
the §24.11 pipeline before persist; the relay-out path also skips
banned sources. Distinct from per-user :table:`user_blocks` — the
ban acts at the federation transport layer where the household
admin overrides social signals.

Stored as a single row per ``instance_id`` with the ban timestamp
and a free-form reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from .base import rows_to_dicts


@dataclass(slots=True, frozen=True)
class HouseholdInstanceBan:
    instance_id: str
    banned_at: str
    reason: str | None


@runtime_checkable
class AbstractHouseholdInstanceBanRepo(Protocol):
    async def add(self, *, instance_id: str, reason: str | None = None) -> None: ...
    async def remove(self, instance_id: str) -> int: ...
    async def is_banned(self, instance_id: str) -> bool: ...
    async def list_all(self) -> list[HouseholdInstanceBan]: ...


class SqliteHouseholdInstanceBanRepo:
    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def add(self, *, instance_id: str, reason: str | None = None) -> None:
        await self._db.enqueue(
            "INSERT INTO household_instance_bans(instance_id, reason) "
            "VALUES(?, ?) "
            "ON CONFLICT(instance_id) DO UPDATE SET reason=excluded.reason",
            (instance_id, reason),
        )

    async def remove(self, instance_id: str) -> int:
        def _run(conn) -> int:
            cur = conn.execute(
                "DELETE FROM household_instance_bans WHERE instance_id=?",
                (instance_id,),
            )
            return int(cur.rowcount or 0)

        return await self._db.transact(_run)

    async def is_banned(self, instance_id: str) -> bool:
        if not instance_id:
            return False
        row = await self._db.fetchone(
            "SELECT 1 FROM household_instance_bans WHERE instance_id=? LIMIT 1",
            (instance_id,),
        )
        return row is not None

    async def list_all(self) -> list[HouseholdInstanceBan]:
        rows = await self._db.fetchall(
            "SELECT instance_id, banned_at, reason "
            "FROM household_instance_bans ORDER BY banned_at DESC",
            (),
        )
        return [
            HouseholdInstanceBan(
                instance_id=r["instance_id"],
                banned_at=r["banned_at"],
                reason=r.get("reason"),
            )
            for r in rows_to_dicts(rows)
        ]
