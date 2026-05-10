"""Peer-user visibility (outbound) — admin-controlled allow / deny list
of which local users surface to a given paired peer.

Default behaviour (no row) = visible. A row with ``visible=0`` hides the
user from the named peer's federation events.

See ``docs/superpowers/specs/2026-05-10-peer-user-visibility-design.md``
for the design rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase


@dataclass(slots=True, frozen=True)
class PeerUserVisibilityRow:
    """One ``peer_user_visibility`` row — used by the route layer when
    rendering the per-pair admin view alongside the local users list.
    Rows for default-visible users (no DB row) are not represented; the
    service synthesises a default row at query time.
    """

    instance_id: str
    user_id: str
    visible: bool
    set_at: str
    set_by: str | None


@runtime_checkable
class AbstractPeerUserVisibilityRepo(Protocol):
    async def is_visible(self, instance_id: str, user_id: str) -> bool: ...
    async def list_for_peer(self, instance_id: str) -> list[PeerUserVisibilityRow]: ...
    async def set_visibility(
        self,
        *,
        instance_id: str,
        user_id: str,
        visible: bool,
        set_by: str | None,
    ) -> None: ...
    async def hidden_user_ids_for_peer(self, instance_id: str) -> frozenset[str]: ...


class SqlitePeerUserVisibilityRepo:
    """SQLite-backed :class:`AbstractPeerUserVisibilityRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def is_visible(self, instance_id: str, user_id: str) -> bool:
        """Default-visible: a missing row means *visible*."""
        row = await self._db.fetchone(
            "SELECT visible FROM peer_user_visibility "
            "WHERE instance_id=? AND user_id=?",
            (instance_id, user_id),
        )
        if row is None:
            return True
        return bool(row["visible"])

    async def list_for_peer(self, instance_id: str) -> list[PeerUserVisibilityRow]:
        rows = await self._db.fetchall(
            "SELECT instance_id, user_id, visible, set_at, set_by "
            "FROM peer_user_visibility WHERE instance_id=?",
            (instance_id,),
        )
        return [
            PeerUserVisibilityRow(
                instance_id=str(r["instance_id"]),
                user_id=str(r["user_id"]),
                visible=bool(r["visible"]),
                set_at=str(r["set_at"]),
                set_by=(str(r["set_by"]) if r["set_by"] is not None else None),
            )
            for r in rows
        ]

    async def hidden_user_ids_for_peer(self, instance_id: str) -> frozenset[str]:
        """Hot-path lookup for the federation outbound filter.

        Returns only the *hidden* user_ids — the default-visible case is
        empty, which is the common case (most pairs have no rows). The
        outbound filter then short-circuits with ``user_id in hidden``.
        """
        rows = await self._db.fetchall(
            "SELECT user_id FROM peer_user_visibility "
            "WHERE instance_id=? AND visible=0",
            (instance_id,),
        )
        return frozenset(str(r["user_id"]) for r in rows)

    async def set_visibility(
        self,
        *,
        instance_id: str,
        user_id: str,
        visible: bool,
        set_by: str | None,
    ) -> None:
        await self._db.enqueue(
            "INSERT INTO peer_user_visibility(instance_id, user_id, visible, set_by) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(instance_id, user_id) DO UPDATE SET "
            "visible=excluded.visible, "
            "set_at=datetime('now'), "
            "set_by=excluded.set_by",
            (instance_id, user_id, 1 if visible else 0, set_by),
        )
