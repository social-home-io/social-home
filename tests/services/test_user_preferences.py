"""Tests for the small user_preferences helper."""

from __future__ import annotations

from socialhome.domain.story import StoryAudience
from socialhome.services.user_preferences import (
    DEFAULT_STORIES_MAX_COUNT,
    DEFAULT_STORIES_RETENTION_DAYS,
    parse_stories_preferences,
)


def test_defaults_when_blob_is_empty():
    p = parse_stories_preferences(None)
    assert p.retention_days == DEFAULT_STORIES_RETENTION_DAYS
    assert p.max_count == DEFAULT_STORIES_MAX_COUNT
    assert p.default_audience_kind is StoryAudience.ALL_PAIRED
    assert p.default_audience == ()


def test_invalid_json_falls_back_to_defaults():
    p = parse_stories_preferences("{not json")
    assert p.retention_days == DEFAULT_STORIES_RETENTION_DAYS


def test_clamps_out_of_range_values():
    """Values outside [_RETENTION_MIN, _MAX] clamp; nonsensical values use default."""
    p = parse_stories_preferences(
        '{"stories": {"retention_days": 99999, "max_count": 2}}'
    )
    assert p.retention_days == 90  # clamped to _RETENTION_MAX
    assert p.max_count == 10  # clamped to _MAX_COUNT_MIN

    p = parse_stories_preferences('{"stories": {"retention_days": "nope"}}')
    assert p.retention_days == DEFAULT_STORIES_RETENTION_DAYS


def test_users_audience_with_ids():
    p = parse_stories_preferences(
        '{"stories": {"default_audience": {"kind": "users", "ids": ["u1", "u2"]}}}'
    )
    assert p.default_audience_kind is StoryAudience.USERS
    assert p.default_audience == ("u1", "u2")


def test_unknown_audience_kind_falls_back():
    p = parse_stories_preferences(
        '{"stories": {"default_audience": {"kind": "everyone"}}}'
    )
    assert p.default_audience_kind is StoryAudience.ALL_PAIRED
    assert p.default_audience == ()


def test_blob_top_level_not_dict():
    """Defensive: when preferences_json itself is a list / scalar."""
    p = parse_stories_preferences('"oops"')
    assert p.retention_days == DEFAULT_STORIES_RETENTION_DAYS


def test_stories_block_not_dict():
    p = parse_stories_preferences('{"stories": "broken"}')
    assert p.retention_days == DEFAULT_STORIES_RETENTION_DAYS
