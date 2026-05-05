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

import re
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


#: Cap on how many tags survive per moment. Twitter is generous on
#: tags-in-a-tweet but most quality timelines stay below 5; clamp here
#: so a tag-spam moment doesn't inflate the trending list.
MOMENT_MAX_HASHTAGS_PER_POST: int = 10

#: Per-tag length cap (chars after the leading ``#``). Anything longer
#: is dropped silently — same shape as the WhatsApp behaviour where
#: the parser only takes contiguous word characters.
MOMENT_MAX_HASHTAG_LEN: int = 32

#: ``#tag`` matcher. Word characters only — no spaces, punctuation,
#: emoji. Matches the URL-safe charset so the SPA can route to
#: ``/momentum/archive?tag=<tag>`` without escaping.
_HASHTAG_RE = re.compile(r"(?<!\w)#([A-Za-z0-9_]{1,%d})" % MOMENT_MAX_HASHTAG_LEN)


def extract_hashtags(content: str) -> list[str]:
    """Return the unique lowercased hashtags found in ``content``.

    * Order is preserved by *first* occurrence in the source so the
      trending query can use the natural insertion order if it ever
      wants to surface "first-seen" semantics.
    * Adjacent ``##foo`` only counts as one tag (``foo``); the negative
      lookbehind on ``\\w`` prevents matching mid-word like ``b##foo``.
    * Capped at :data:`MOMENT_MAX_HASHTAGS_PER_POST`.
    """
    if not content:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _HASHTAG_RE.finditer(content):
        tag = m.group(1).lower()
        if tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
        if len(out) >= MOMENT_MAX_HASHTAGS_PER_POST:
            break
    return out
