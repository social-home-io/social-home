"""Tiny helper for parsing the ``users.preferences_json`` JSON blob.

The blob is owned by the frontend (free-form keys), but a handful of
backend services read it: notification preferences, retention, etc.
This module centralises parsing so callers don't reinvent the
defensive defaults each time.

For now there's only the :class:`StoriesPreferences` shape; add more as
they appear.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from ..domain.story import StoryAudience

log = logging.getLogger(__name__)


#: Default "how many days to keep a story listed" if the user has not
#: configured a value. Long enough to feel different from WhatsApp's
#: 24-hour wipe; short enough that the inbox doesn't grow unbounded.
DEFAULT_STORIES_RETENTION_DAYS: int = 30
#: Default ceiling on stored stories per author. The retention scheduler
#: prunes the oldest beyond this count even if they have not yet expired.
DEFAULT_STORIES_MAX_COUNT: int = 100

_RETENTION_MIN: int = 1
_RETENTION_MAX: int = 90
_MAX_COUNT_MIN: int = 10
_MAX_COUNT_MAX: int = 500


@dataclass(slots=True, frozen=True)
class StoriesPreferences:
    """Parsed ``preferences_json["stories"]`` block (always populated)."""

    retention_days: int
    max_count: int
    default_audience_kind: StoryAudience
    default_audience: tuple[str, ...]


def parse_stories_preferences(preferences_json: str | None) -> StoriesPreferences:
    """Pull the ``stories`` block out of a user's preferences blob.

    Always returns a well-formed :class:`StoriesPreferences` — bad / missing
    inputs fall back to the defaults defined above. Out-of-range numeric
    values are clamped (so a malformed blob can never DOS the retention
    scheduler with e.g. ``retention_days = 10**9``).
    """
    blob: dict[str, Any] = {}
    try:
        blob = json.loads(preferences_json or "{}") if preferences_json else {}
    except json.JSONDecodeError:
        log.debug("preferences_json is not valid JSON; using defaults")
        blob = {}
    if not isinstance(blob, dict):
        blob = {}
    raw = blob.get("stories")
    if not isinstance(raw, dict):
        raw = {}

    retention_days = _coerce_int(
        raw.get("retention_days"),
        DEFAULT_STORIES_RETENTION_DAYS,
        _RETENTION_MIN,
        _RETENTION_MAX,
    )
    max_count = _coerce_int(
        raw.get("max_count"),
        DEFAULT_STORIES_MAX_COUNT,
        _MAX_COUNT_MIN,
        _MAX_COUNT_MAX,
    )

    audience_block = raw.get("default_audience")
    if not isinstance(audience_block, dict):
        audience_block = {"kind": "all_paired"}
    try:
        kind = StoryAudience(audience_block.get("kind") or "all_paired")
    except ValueError:
        kind = StoryAudience.ALL_PAIRED
    ids_raw = audience_block.get("ids")
    if not isinstance(ids_raw, list):
        ids_raw = []
    ids: tuple[str, ...] = tuple(str(x) for x in ids_raw if x)

    return StoriesPreferences(
        retention_days=retention_days,
        max_count=max_count,
        default_audience_kind=kind,
        default_audience=ids if kind is not StoryAudience.ALL_PAIRED else (),
    )


def _coerce_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < lo:
        return lo
    if n > hi:
        return hi
    return n


__all__ = [
    "DEFAULT_STORIES_MAX_COUNT",
    "DEFAULT_STORIES_RETENTION_DAYS",
    "StoriesPreferences",
    "parse_stories_preferences",
]
