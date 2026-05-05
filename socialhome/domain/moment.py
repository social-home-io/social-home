"""Domain types for the Momentum pillar (§Momentum).

A *moment* is a one-shot household-broadcast post: ≤ 1 000 chars text
plus an optional image or short (≤ 15 s) video. Moments fan out to
every paired peer and their peers, up to 3 hops. They live 24 h by
default; if a local viewer has explicitly followed the author the
moment surfaces in their inbox up to 7 d (the same row, just a longer
visible window — retention is per-viewer at the list-query layer).

Replies are themselves moments and carry a ``parent_moment_id``. The
reply inherits the parent's audience implicitly via the federation
relay: the parent already reached every relevant peer mesh, so the
reply re-uses the same fan-out as a top-level moment from the same
author. Threading stays flat (replies-to-replies attach to the same
root parent_moment_id).
"""

from __future__ import annotations

from dataclasses import dataclass


#: Hard caps. Composer + service both enforce.
MOMENT_MAX_CONTENT_LEN: int = 1_000
MOMENT_MAX_VIDEO_MS: int = 15_000

#: Federation relay: cap at 3 hops. Origin sends with hop=1; each
#: receiver bumps and re-fans up to ``MAX_HOPS - 1``.
MOMENT_MAX_HOPS: int = 3


@dataclass(slots=True, frozen=True)
class Moment:
    """A single broadcast post.

    ``author_user_id`` is plain text (no FK) so remote authors land in
    the shared ``moments`` table — same convention as
    ``conversation_messages.sender_user_id`` for DMs.

    ``parent_moment_id`` is ``None`` for top-level moments.
    """

    id: str
    author_user_id: str
    content: str
    media_url: str | None
    media_type: str | None  # 'image' | 'video' | None
    duration_ms: int | None
    parent_moment_id: str | None
    origin_instance_id: str
    created_at: str
    expires_at: str


@dataclass(slots=True, frozen=True)
class MomentReaction:
    """One reactor's per-moment emoji. UPSERT keyed on (moment_id, reactor)."""

    moment_id: str
    reactor_user_id: str
    emoji: str
    reacted_at: str
