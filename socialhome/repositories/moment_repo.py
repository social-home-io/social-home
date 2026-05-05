"""Moment repository — persistence for the Momentum pillar (§Momentum).

Manages two tables:

* ``moments`` — per-author broadcast posts (text + optional image /
  short video). Holds local AND federated remote-author rows; the
  ``author_user_id`` column has no FK so peers' content can land
  alongside local content (same convention as
  ``conversation_messages.sender_user_id``).
* ``moment_reactions`` — one row per (moment_id, reactor_user_id),
  holding the current emoji.

The visibility query in :meth:`list_visible_to` does *all* the
filtering in one statement: drop blocked authors, collapse the
absolute 7-day retention to 24 h for non-followers, and apply the
``before`` cursor for pagination.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.moment import Moment, MomentReaction
from .base import row_to_dict, rows_to_dicts


@runtime_checkable
class AbstractMomentRepo(Protocol):
    async def save(self, moment: Moment) -> Moment: ...
    async def get(self, moment_id: str) -> Moment | None: ...
    async def delete(self, moment_id: str) -> None: ...
    async def list_visible_to(
        self,
        viewer_user_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
    ) -> list[Moment]: ...
    async def list_replies(self, parent_moment_id: str) -> list[Moment]: ...
    async def count_recent_for_author(
        self,
        author_user_id: str,
        *,
        since_iso: str,
    ) -> int: ...

    # Reactions -----------------------------------------------------------
    async def set_reaction(
        self,
        moment_id: str,
        reactor_user_id: str,
        emoji: str,
    ) -> None: ...
    async def clear_reaction(
        self,
        moment_id: str,
        reactor_user_id: str,
    ) -> None: ...
    async def list_reactions(self, moment_id: str) -> list[MomentReaction]: ...

    # Engagement counters --------------------------------------------------
    async def count_engagement_for(
        self,
        moment_ids: list[str],
    ) -> dict[str, dict[str, int]]: ...

    # Retention -----------------------------------------------------------
    async def prune_expired(self) -> int: ...


class SqliteMomentRepo:
    """SQLite-backed :class:`AbstractMomentRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def save(self, moment: Moment) -> Moment:
        """Upsert by id. Inbound federation handlers call this with
        the dedup-by-id semantics the relay needs."""
        await self._db.enqueue(
            """
            INSERT INTO moments(
                id, author_user_id, content, media_url, media_type,
                duration_ms, parent_moment_id, origin_instance_id,
                created_at, expires_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                content=excluded.content,
                media_url=excluded.media_url,
                media_type=excluded.media_type,
                duration_ms=excluded.duration_ms,
                expires_at=excluded.expires_at
            """,
            (
                moment.id,
                moment.author_user_id,
                moment.content,
                moment.media_url,
                moment.media_type,
                moment.duration_ms,
                moment.parent_moment_id,
                moment.origin_instance_id,
                moment.created_at,
                moment.expires_at,
            ),
        )
        return moment

    async def get(self, moment_id: str) -> Moment | None:
        row = await self._db.fetchone(
            "SELECT * FROM moments WHERE id=?",
            (moment_id,),
        )
        return _row_to_moment(row_to_dict(row))

    async def delete(self, moment_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM moments WHERE id=?",
            (moment_id,),
        )

    async def list_visible_to(
        self,
        viewer_user_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
    ) -> list[Moment]:
        """One query covers blocks, retention, and the cursor.

        Visibility:
        * Author is not on the viewer's :table:`user_blocks`.
        * EITHER the moment is < 24 h old, OR the viewer follows the
          author (then up to 7 d, the absolute ``expires_at``).
        * ``before`` is an ISO-8601 ``created_at`` cursor; ``NULL``
          fetches from newest.
        """
        limit = max(1, min(int(limit), 100))
        rows = await self._db.fetchall(
            """
            SELECT * FROM moments
             WHERE author_user_id NOT IN (
                 SELECT blocked_user_id FROM user_blocks
                  WHERE blocker_user_id = ?
             )
               AND (
                 (julianday('now') - julianday(created_at)) * 24 < 24
                 OR EXISTS (
                     SELECT 1 FROM user_follows
                      WHERE follower_user_id = ?
                        AND followed_user_id = moments.author_user_id
                 )
               )
               AND expires_at > datetime('now')
               AND (? IS NULL OR created_at < ?)
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (viewer_user_id, viewer_user_id, before, before, limit),
        )
        return [m for m in (_row_to_moment(d) for d in rows_to_dicts(rows)) if m]

    async def list_replies(self, parent_moment_id: str) -> list[Moment]:
        """Replies in chronological order so the thread reads top-down."""
        rows = await self._db.fetchall(
            "SELECT * FROM moments WHERE parent_moment_id=? ORDER BY created_at ASC",
            (parent_moment_id,),
        )
        return [m for m in (_row_to_moment(d) for d in rows_to_dicts(rows)) if m]

    async def count_recent_for_author(
        self,
        author_user_id: str,
        *,
        since_iso: str,
    ) -> int:
        """Top-level moments only — replies are exempt from rate-limit."""
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM moments "
            "WHERE author_user_id=? AND created_at>=? "
            "AND parent_moment_id IS NULL",
            (author_user_id, since_iso),
        )
        return int(row["n"]) if row else 0

    # ── Reactions ──────────────────────────────────────────────────────

    async def set_reaction(
        self,
        moment_id: str,
        reactor_user_id: str,
        emoji: str,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO moment_reactions(moment_id, reactor_user_id, emoji)
            VALUES(?, ?, ?)
            ON CONFLICT(moment_id, reactor_user_id) DO UPDATE SET
                emoji=excluded.emoji,
                reacted_at=datetime('now')
            """,
            (moment_id, reactor_user_id, emoji),
        )

    async def clear_reaction(
        self,
        moment_id: str,
        reactor_user_id: str,
    ) -> None:
        await self._db.enqueue(
            "DELETE FROM moment_reactions WHERE moment_id=? AND reactor_user_id=?",
            (moment_id, reactor_user_id),
        )

    async def list_reactions(self, moment_id: str) -> list[MomentReaction]:
        rows = await self._db.fetchall(
            "SELECT moment_id, reactor_user_id, emoji, reacted_at "
            "FROM moment_reactions WHERE moment_id=? "
            "ORDER BY reacted_at ASC",
            (moment_id,),
        )
        return [
            MomentReaction(
                moment_id=r["moment_id"],
                reactor_user_id=r["reactor_user_id"],
                emoji=r["emoji"],
                reacted_at=r["reacted_at"],
            )
            for r in rows
        ]

    # ── Engagement counters ────────────────────────────────────────────

    async def count_engagement_for(
        self,
        moment_ids: list[str],
    ) -> dict[str, dict[str, int]]:
        """Aggregate reactions + replies for the given moment ids.

        Returns ``{moment_id: {"reaction_count": N, "reply_count": M}}``
        with zeros for ids that have no rows. One trip per table —
        cheap enough for the typical inbox page (≤ 50 ids).

        The Twitter-style row layout shows these counts inline, so the
        SPA can render the chip row without a per-row follow-up fetch.
        """
        out: dict[str, dict[str, int]] = {
            mid: {"reaction_count": 0, "reply_count": 0} for mid in moment_ids
        }
        if not moment_ids:
            return out
        placeholders = ",".join("?" for _ in moment_ids)
        rxn_rows = await self._db.fetchall(
            f"""
            SELECT moment_id, COUNT(*) AS n
              FROM moment_reactions
             WHERE moment_id IN ({placeholders})
             GROUP BY moment_id
            """,
            tuple(moment_ids),
        )
        for r in rxn_rows:
            out[r["moment_id"]]["reaction_count"] = int(r["n"])
        rep_rows = await self._db.fetchall(
            f"""
            SELECT parent_moment_id AS moment_id, COUNT(*) AS n
              FROM moments
             WHERE parent_moment_id IN ({placeholders})
             GROUP BY parent_moment_id
            """,
            tuple(moment_ids),
        )
        for r in rep_rows:
            out[r["moment_id"]]["reply_count"] = int(r["n"])
        return out

    # ── Retention ──────────────────────────────────────────────────────

    async def prune_expired(self) -> int:
        """Drop rows past their absolute 7-day cap. Reactions cascade."""
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM moments WHERE expires_at < datetime('now')",
        )
        n = int(row["n"]) if row else 0
        if n == 0:
            return 0
        await self._db.enqueue(
            "DELETE FROM moments WHERE expires_at < datetime('now')",
        )
        return n


def _row_to_moment(row: dict | None) -> Moment | None:
    if row is None:
        return None
    return Moment(
        id=row["id"],
        author_user_id=row["author_user_id"],
        content=row.get("content") or "",
        media_url=row.get("media_url"),
        media_type=row.get("media_type"),
        duration_ms=row.get("duration_ms"),
        parent_moment_id=row.get("parent_moment_id"),
        origin_instance_id=row["origin_instance_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )
