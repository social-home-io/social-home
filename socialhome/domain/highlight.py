"""Highlight domain types (§Highlights).

A Highlight is a per-author per-day bag of frames (image / short video) that
federates to peers based on an author-controlled audience. Day-grouping
is enforced at the schema level: ``UNIQUE(author_user_id, highlight_date)``
on the ``highlights`` table — two posts on the same day append frames to a
shared row, a fresh day creates a new row.

Retention is author-controlled. The author's ``users.preferences_json``
carries ``highlights.retention_days`` and ``highlights.max_count``; the
:class:`HighlightRetentionScheduler` prunes ``highlights`` rows where
``expires_at`` lies in the past, plus the oldest highlights per author once
their max-count is exceeded.

Audience can be one of three kinds (see :class:`HighlightAudience`):

- ``ALL_PAIRED`` — all confirmed peer instances; default.
- ``HOUSEHOLDS`` — listed peer instance ids only.
- ``USERS``      — listed individual user ids; the receiving instance
  enforces the user allow-list before surfacing.

All dataclasses are frozen + slotted per the project's domain rules
(``CLAUDE.md``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class HighlightAudience(StrEnum):
    """Audience kinds for a highlight (see ``highlights.audience_kind``)."""

    ALL_PAIRED = "all_paired"
    HOUSEHOLDS = "households"
    USERS = "users"


class HighlightFrameType(StrEnum):
    """Media type of a single frame."""

    IMAGE = "image"
    VIDEO = "video"


@dataclass(slots=True, frozen=True)
class Highlight:
    """A per-author per-day highlight row."""

    id: str
    author_user_id: str
    #: ``YYYY-MM-DD`` UTC.
    highlight_date: str
    audience_kind: HighlightAudience = HighlightAudience.ALL_PAIRED
    #: Empty for ``ALL_PAIRED``; list of instance_ids or user_ids
    #: depending on ``audience_kind``.
    audience: tuple[str, ...] = field(default_factory=tuple)
    created_at: str | None = None
    #: ISO-8601 UTC. Highlight is purged by the retention scheduler once
    #: the wall-clock passes this point.
    expires_at: str | None = None
    #: When the author opts to share the highlight via a GFS (§highlights_public),
    #: the GFS connection id used. ``None`` while not published. Cleared
    #: by ``unpublish``. Tokens (one per share link) live on the GFS;
    #: SH only knows whether *some* publication exists.
    public_gfs_id: str | None = None
    #: ISO-8601 UTC of the original publish call. Persisted so the SPA
    #: can show "published 12 minutes ago" without an extra GFS round-trip.
    public_published_at: str | None = None


@dataclass(slots=True, frozen=True)
class HighlightFrame:
    """One frame inside a highlight (image or short video)."""

    id: str
    highlight_id: str
    sequence: int
    frame_type: HighlightFrameType
    #: Canonical ``/api/media/{filename}`` path. Server re-signs at read.
    media_url: str
    caption_text: str | None = None
    caption_emoji: str | None = None
    duration_ms: int | None = None
    created_at: str | None = None


@dataclass(slots=True, frozen=True)
class HighlightFrameView:
    """Per-viewer per-frame view record. Drives "viewed by …" UX."""

    frame_id: str
    viewer_user_id: str
    viewed_at: str | None = None


@dataclass(slots=True, frozen=True)
class HighlightFrameReaction:
    """Per-viewer per-frame reaction. One per viewer per frame; UPSERT."""

    frame_id: str
    reactor_user_id: str
    emoji: str
    reacted_at: str | None = None


@dataclass(slots=True, frozen=True)
class HighlightFrameReplySnapshot:
    """JSON snapshot frozen onto a DM message that replies to a frame.

    Stored as JSON in
    ``conversation_messages.reply_to_highlight_frame_snapshot`` so the reply
    stays meaningful after the underlying frame is removed by the
    retention scheduler.
    """

    thumb_url: str
    author_user_id: str
    highlight_date: str
    caption_text: str | None = None
    caption_emoji: str | None = None
