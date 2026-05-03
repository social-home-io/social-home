"""Story repository (§Stories).

Backs :class:`StoryService` with SQLite I/O. Holds no business logic
beyond the queries — see ``services/story_service.py`` for the
"create-or-append-today", "fan out to peers", and "expire by retention"
flows.

Audience evaluation lives here only as a query filter: outbound
federation fan-out is done at the service layer (it needs to know about
:class:`RemoteInstance`s, which the repo does not). Receivers that
shouldn't see a story drop the inbound envelope before it reaches the
repo at all, so by the time a row exists locally we treat all
on-instance users as "in audience" — except for ``USERS``-kind stories,
where the per-user allow-list is enforced on read.
"""

from __future__ import annotations

import builtins
import uuid
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.story import (
    Story,
    StoryAudience,
    StoryFrame,
    StoryFrameReaction,
    StoryFrameType,
    StoryFrameView,
)
from .base import dump_json, load_json, row_to_dict, rows_to_dicts


@runtime_checkable
class AbstractStoryRepo(Protocol):
    async def find_or_create_today(
        self,
        *,
        author_user_id: str,
        audience_kind: StoryAudience,
        audience: builtins.tuple[str, ...],
        story_date: str,
        expires_at: str,
    ) -> Story: ...
    async def append_frame(
        self,
        *,
        story_id: str,
        frame_type: StoryFrameType,
        media_url: str,
        caption_text: str | None = None,
        caption_emoji: str | None = None,
        duration_ms: int | None = None,
    ) -> StoryFrame: ...
    async def get_story(self, story_id: str) -> Story | None: ...
    async def get_frame(self, frame_id: str) -> StoryFrame | None: ...
    async def list_frames(self, story_id: str) -> builtins.list[StoryFrame]: ...
    async def list_visible_to(
        self,
        viewer_user_id: str,
    ) -> builtins.list[Story]: ...
    async def list_authored(
        self,
        author_user_id: str,
        *,
        limit: int = 50,
    ) -> builtins.list[Story]: ...
    async def mark_viewed(self, frame_id: str, viewer_user_id: str) -> None: ...
    async def list_views_for_frame(
        self,
        frame_id: str,
    ) -> builtins.list[StoryFrameView]: ...
    async def count_unseen_frames(
        self,
        story_id: str,
        viewer_user_id: str,
    ) -> int: ...
    async def set_reaction(
        self,
        frame_id: str,
        reactor_user_id: str,
        emoji: str,
    ) -> StoryFrameReaction: ...
    async def clear_reaction(
        self,
        frame_id: str,
        reactor_user_id: str,
    ) -> None: ...
    async def list_reactions_for_frame(
        self,
        frame_id: str,
    ) -> builtins.list[StoryFrameReaction]: ...
    async def delete_frame(self, frame_id: str) -> None: ...
    async def delete_story(self, story_id: str) -> None: ...
    async def prune_expired(self) -> int: ...
    async def prune_over_max(self, author_user_id: str, max_count: int) -> int: ...
    async def save_story(self, story: Story) -> Story: ...
    async def save_frame(self, frame: StoryFrame) -> StoryFrame: ...
    async def list_authors_with_stories(self) -> builtins.list[str]: ...


class SqliteStoryRepo:
    """SQLite-backed :class:`AbstractStoryRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Story rows ───────────────────────────────────────────────────────

    async def find_or_create_today(
        self,
        *,
        author_user_id: str,
        audience_kind: StoryAudience,
        audience: tuple[str, ...],
        story_date: str,
        expires_at: str,
    ) -> Story:
        existing = await self._db.fetchone(
            "SELECT * FROM stories WHERE author_user_id=? AND story_date=?",
            (author_user_id, story_date),
        )
        if existing is not None:
            return _row_to_story(row_to_dict(existing))  # type: ignore[return-value]
        story = Story(
            id=uuid.uuid4().hex,
            author_user_id=author_user_id,
            story_date=story_date,
            audience_kind=audience_kind,
            audience=tuple(audience),
            created_at=datetime.now(timezone.utc).isoformat(),
            expires_at=expires_at,
        )
        await self._db.enqueue(
            """
            INSERT INTO stories(
                id, author_user_id, story_date, audience_kind, audience_json,
                created_at, expires_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                story.id,
                story.author_user_id,
                story.story_date,
                story.audience_kind.value,
                dump_json(list(story.audience)),
                story.created_at,
                story.expires_at,
            ),
        )
        return story

    async def get_story(self, story_id: str) -> Story | None:
        row = await self._db.fetchone(
            "SELECT * FROM stories WHERE id=?",
            (story_id,),
        )
        return _row_to_story(row_to_dict(row))

    async def list_visible_to(self, viewer_user_id: str) -> list[Story]:
        # Visible if:
        #   - author is local OR audience_kind in ('all_paired','households'),
        #   - or audience_kind='users' AND viewer in the user allow-list.
        # The receiver dropped envelopes outside the audience already, so
        # by the time we read locally we trust ``stories`` rows. The
        # ``USERS``-kind filter still applies because one row can be
        # surfaced to a strict subset of an instance's users.
        rows = await self._db.fetchall(
            """
            SELECT * FROM stories
            WHERE expires_at > datetime('now')
              AND (
                  audience_kind IN ('all_paired','households')
                  OR (audience_kind = 'users' AND EXISTS (
                      SELECT 1 FROM json_each(stories.audience_json) j
                      WHERE j.value = ?
                  ))
                  OR author_user_id = ?
              )
            ORDER BY story_date DESC, created_at DESC
            """,
            (viewer_user_id, viewer_user_id),
        )
        return [s for s in (_row_to_story(d) for d in rows_to_dicts(rows)) if s]

    async def list_authored(
        self,
        author_user_id: str,
        *,
        limit: int = 50,
    ) -> list[Story]:
        rows = await self._db.fetchall(
            "SELECT * FROM stories WHERE author_user_id=? "
            "ORDER BY story_date DESC, created_at DESC LIMIT ?",
            (author_user_id, int(limit)),
        )
        return [s for s in (_row_to_story(d) for d in rows_to_dicts(rows)) if s]

    async def list_authors_with_stories(self) -> list[str]:
        rows = await self._db.fetchall(
            "SELECT DISTINCT author_user_id FROM stories",
        )
        return [r["author_user_id"] for r in rows_to_dicts(rows)]

    # ── Frames ──────────────────────────────────────────────────────────

    async def append_frame(
        self,
        *,
        story_id: str,
        frame_type: StoryFrameType,
        media_url: str,
        caption_text: str | None = None,
        caption_emoji: str | None = None,
        duration_ms: int | None = None,
    ) -> StoryFrame:
        next_seq_row = await self._db.fetchone(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_seq "
            "FROM story_frames WHERE story_id=?",
            (story_id,),
        )
        next_seq = int(next_seq_row["next_seq"]) if next_seq_row else 1
        frame = StoryFrame(
            id=uuid.uuid4().hex,
            story_id=story_id,
            sequence=next_seq,
            frame_type=frame_type,
            media_url=media_url,
            caption_text=caption_text,
            caption_emoji=caption_emoji,
            duration_ms=duration_ms,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._db.enqueue(
            """
            INSERT INTO story_frames(
                id, story_id, sequence, frame_type, media_url,
                caption_text, caption_emoji, duration_ms, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                frame.id,
                frame.story_id,
                frame.sequence,
                frame.frame_type.value,
                frame.media_url,
                frame.caption_text,
                frame.caption_emoji,
                frame.duration_ms,
                frame.created_at,
            ),
        )
        return frame

    async def get_frame(self, frame_id: str) -> StoryFrame | None:
        row = await self._db.fetchone(
            "SELECT * FROM story_frames WHERE id=?",
            (frame_id,),
        )
        return _row_to_frame(row_to_dict(row))

    async def list_frames(self, story_id: str) -> list[StoryFrame]:
        rows = await self._db.fetchall(
            "SELECT * FROM story_frames WHERE story_id=? ORDER BY sequence ASC",
            (story_id,),
        )
        return [f for f in (_row_to_frame(d) for d in rows_to_dicts(rows)) if f]

    async def delete_frame(self, frame_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM story_frames WHERE id=?",
            (frame_id,),
        )

    async def delete_story(self, story_id: str) -> None:
        # Cascades to frames/views/reactions via FK.
        await self._db.enqueue(
            "DELETE FROM stories WHERE id=?",
            (story_id,),
        )

    # ── Views ───────────────────────────────────────────────────────────

    async def mark_viewed(self, frame_id: str, viewer_user_id: str) -> None:
        await self._db.enqueue(
            """
            INSERT INTO story_frame_views(frame_id, viewer_user_id, viewed_at)
            VALUES(?, ?, datetime('now'))
            ON CONFLICT(frame_id, viewer_user_id) DO NOTHING
            """,
            (frame_id, viewer_user_id),
        )

    async def list_views_for_frame(
        self,
        frame_id: str,
    ) -> list[StoryFrameView]:
        rows = await self._db.fetchall(
            "SELECT * FROM story_frame_views WHERE frame_id=? "
            "ORDER BY viewed_at ASC",
            (frame_id,),
        )
        return [
            StoryFrameView(
                frame_id=r["frame_id"],
                viewer_user_id=r["viewer_user_id"],
                viewed_at=r["viewed_at"],
            )
            for r in rows_to_dicts(rows)
        ]

    async def count_unseen_frames(
        self,
        story_id: str,
        viewer_user_id: str,
    ) -> int:
        row = await self._db.fetchone(
            """
            SELECT COUNT(*) AS c
            FROM story_frames f
            LEFT JOIN story_frame_views v
              ON v.frame_id = f.id AND v.viewer_user_id = ?
            WHERE f.story_id = ? AND v.frame_id IS NULL
            """,
            (viewer_user_id, story_id),
        )
        return int(row["c"]) if row is not None else 0

    # ── Reactions ───────────────────────────────────────────────────────

    async def set_reaction(
        self,
        frame_id: str,
        reactor_user_id: str,
        emoji: str,
    ) -> StoryFrameReaction:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.enqueue(
            """
            INSERT INTO story_frame_reactions(
                frame_id, reactor_user_id, emoji, reacted_at
            ) VALUES(?, ?, ?, ?)
            ON CONFLICT(frame_id, reactor_user_id) DO UPDATE SET
                emoji = excluded.emoji,
                reacted_at = excluded.reacted_at
            """,
            (frame_id, reactor_user_id, emoji, now),
        )
        return StoryFrameReaction(
            frame_id=frame_id,
            reactor_user_id=reactor_user_id,
            emoji=emoji,
            reacted_at=now,
        )

    async def clear_reaction(
        self,
        frame_id: str,
        reactor_user_id: str,
    ) -> None:
        await self._db.enqueue(
            "DELETE FROM story_frame_reactions "
            "WHERE frame_id=? AND reactor_user_id=?",
            (frame_id, reactor_user_id),
        )

    async def list_reactions_for_frame(
        self,
        frame_id: str,
    ) -> list[StoryFrameReaction]:
        rows = await self._db.fetchall(
            "SELECT * FROM story_frame_reactions WHERE frame_id=? "
            "ORDER BY reacted_at ASC",
            (frame_id,),
        )
        return [
            StoryFrameReaction(
                frame_id=r["frame_id"],
                reactor_user_id=r["reactor_user_id"],
                emoji=r["emoji"],
                reacted_at=r["reacted_at"],
            )
            for r in rows_to_dicts(rows)
        ]

    # ── Retention ───────────────────────────────────────────────────────

    async def prune_expired(self) -> int:
        rows = await self._db.fetchall(
            "SELECT id FROM stories WHERE expires_at < datetime('now')",
        )
        ids = [r["id"] for r in rows_to_dicts(rows)]
        for sid in ids:
            await self._db.enqueue("DELETE FROM stories WHERE id=?", (sid,))
        return len(ids)

    async def prune_over_max(
        self,
        author_user_id: str,
        max_count: int,
    ) -> int:
        if max_count <= 0:
            return 0
        rows = await self._db.fetchall(
            "SELECT id FROM stories WHERE author_user_id=? "
            "ORDER BY story_date DESC, created_at DESC",
            (author_user_id,),
        )
        ids = [r["id"] for r in rows_to_dicts(rows)]
        too_old = ids[max_count:]
        for sid in too_old:
            await self._db.enqueue("DELETE FROM stories WHERE id=?", (sid,))
        return len(too_old)

    # ── Federation upserts (preserve remote ids) ────────────────────────

    async def save_story(self, story: Story) -> Story:
        await self._db.enqueue(
            """
            INSERT INTO stories(
                id, author_user_id, story_date, audience_kind, audience_json,
                created_at, expires_at
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                audience_kind = excluded.audience_kind,
                audience_json = excluded.audience_json,
                expires_at    = excluded.expires_at
            """,
            (
                story.id,
                story.author_user_id,
                story.story_date,
                story.audience_kind.value,
                dump_json(list(story.audience)),
                story.created_at or datetime.now(timezone.utc).isoformat(),
                story.expires_at,
            ),
        )
        return story

    async def save_frame(self, frame: StoryFrame) -> StoryFrame:
        await self._db.enqueue(
            """
            INSERT INTO story_frames(
                id, story_id, sequence, frame_type, media_url,
                caption_text, caption_emoji, duration_ms, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                sequence      = excluded.sequence,
                frame_type    = excluded.frame_type,
                media_url     = excluded.media_url,
                caption_text  = excluded.caption_text,
                caption_emoji = excluded.caption_emoji,
                duration_ms   = excluded.duration_ms
            """,
            (
                frame.id,
                frame.story_id,
                frame.sequence,
                frame.frame_type.value,
                frame.media_url,
                frame.caption_text,
                frame.caption_emoji,
                frame.duration_ms,
                frame.created_at or datetime.now(timezone.utc).isoformat(),
            ),
        )
        return frame


def _row_to_story(row: dict | None) -> Story | None:
    if row is None:
        return None
    audience_raw = load_json(row.get("audience_json"), [])
    if not isinstance(audience_raw, list):
        audience_raw = []
    audience: tuple[str, ...] = tuple(str(x) for x in audience_raw)
    try:
        kind = StoryAudience(row.get("audience_kind") or "all_paired")
    except ValueError:
        kind = StoryAudience.ALL_PAIRED
    return Story(
        id=row["id"],
        author_user_id=row["author_user_id"],
        story_date=row["story_date"],
        audience_kind=kind,
        audience=audience,
        created_at=row.get("created_at"),
        expires_at=row.get("expires_at"),
    )


def _row_to_frame(row: dict | None) -> StoryFrame | None:
    if row is None:
        return None
    try:
        ftype = StoryFrameType(row["frame_type"])
    except (KeyError, ValueError):
        return None
    return StoryFrame(
        id=row["id"],
        story_id=row["story_id"],
        sequence=int(row["sequence"]),
        frame_type=ftype,
        media_url=row["media_url"],
        caption_text=row.get("caption_text"),
        caption_emoji=row.get("caption_emoji"),
        duration_ms=(
            int(row["duration_ms"]) if row.get("duration_ms") is not None else None
        ),
        created_at=row.get("created_at"),
    )


__all__ = ["AbstractStoryRepo", "SqliteStoryRepo"]
