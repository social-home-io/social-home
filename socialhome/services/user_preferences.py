"""Tiny helper for parsing the ``users.preferences_json`` JSON blob.

The blob is owned by the frontend (free-form keys), but a handful of
backend services read it: notification preferences, retention, etc.
This module centralises parsing so callers don't reinvent the
defensive defaults each time.

For now there's only the :class:`HighlightsPreferences` shape; add more as
they appear.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from ..domain.highlight import HighlightAudience

log = logging.getLogger(__name__)


#: Default "how many days to keep a highlight listed" if the user has not
#: configured a value. Long enough to feel different from WhatsApp's
#: 24-hour wipe; short enough that the inbox doesn't grow unbounded.
DEFAULT_HIGHLIGHTS_RETENTION_DAYS: int = 30
#: Default ceiling on stored highlights per author. The retention scheduler
#: prunes the oldest beyond this count even if they have not yet expired.
DEFAULT_HIGHLIGHTS_MAX_COUNT: int = 100

_RETENTION_MIN: int = 1
_RETENTION_MAX: int = 90
_MAX_COUNT_MIN: int = 10
_MAX_COUNT_MAX: int = 500


@dataclass(slots=True, frozen=True)
class HighlightsPreferences:
    """Parsed ``preferences_json["highlights"]`` block (always populated)."""

    retention_days: int
    max_count: int
    default_audience_kind: HighlightAudience
    default_audience: tuple[str, ...]


def parse_highlights_preferences(preferences_json: str | None) -> HighlightsPreferences:
    """Pull the ``highlights`` block out of a user's preferences blob.

    Always returns a well-formed :class:`HighlightsPreferences` — bad / missing
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
    raw = blob.get("highlights")
    if not isinstance(raw, dict):
        raw = {}

    retention_days = _coerce_int(
        raw.get("retention_days"),
        DEFAULT_HIGHLIGHTS_RETENTION_DAYS,
        _RETENTION_MIN,
        _RETENTION_MAX,
    )
    max_count = _coerce_int(
        raw.get("max_count"),
        DEFAULT_HIGHLIGHTS_MAX_COUNT,
        _MAX_COUNT_MIN,
        _MAX_COUNT_MAX,
    )

    audience_block = raw.get("default_audience")
    if not isinstance(audience_block, dict):
        audience_block = {"kind": "all_paired"}
    try:
        kind = HighlightAudience(audience_block.get("kind") or "all_paired")
    except ValueError:
        kind = HighlightAudience.ALL_PAIRED
    ids_raw = audience_block.get("ids")
    if not isinstance(ids_raw, list):
        ids_raw = []
    ids: tuple[str, ...] = tuple(str(x) for x in ids_raw if x)

    return HighlightsPreferences(
        retention_days=retention_days,
        max_count=max_count,
        default_audience_kind=kind,
        default_audience=ids if kind is not HighlightAudience.ALL_PAIRED else (),
    )


def _coerce_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except TypeError, ValueError:
        return default
    if n < lo:
        return lo
    if n > hi:
        return hi
    return n


# ─── Momentum visibility prefs (§Momentum-relay-policy) ─────────────────────


#: Default per-user max-hops visibility — matches the global wire cap so
#: new accounts see every moment that reached this instance.
DEFAULT_MOMENT_MAX_HOPS: int = 3


@dataclass(slots=True, frozen=True)
class MomentPreferences:
    """Per-user knobs for the Momentum pillar's visibility filter."""

    max_hops: int


def parse_moment_preferences(preferences_json: str | None) -> MomentPreferences:
    """Pull the ``moments`` block out of a user's preferences blob.

    Returns a well-formed :class:`MomentPreferences` for any input.
    Out-of-range integers clamp to ``[1, 3]`` since the wire cap is 3
    and a value < 1 would hide every moment including the user's own.
    """
    blob: dict[str, Any] = {}
    try:
        blob = json.loads(preferences_json or "{}") if preferences_json else {}
    except json.JSONDecodeError:
        log.debug("preferences_json is not valid JSON; using defaults")
        blob = {}
    if not isinstance(blob, dict):
        blob = {}
    raw = blob.get("moments")
    if not isinstance(raw, dict):
        raw = {}
    max_hops = _coerce_int(raw.get("max_hops"), DEFAULT_MOMENT_MAX_HOPS, 1, 3)
    return MomentPreferences(max_hops=max_hops)


__all__ = [
    "DEFAULT_HIGHLIGHTS_MAX_COUNT",
    "DEFAULT_HIGHLIGHTS_RETENTION_DAYS",
    "DEFAULT_MOMENT_MAX_HOPS",
    "HighlightsPreferences",
    "MomentPreferences",
    "parse_highlights_preferences",
    "parse_moment_preferences",
]
