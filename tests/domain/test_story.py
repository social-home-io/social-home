"""Smoke tests for the Story domain dataclasses."""

from __future__ import annotations

import dataclasses

from socialhome.domain.story import (
    Story,
    StoryAudience,
    StoryFrame,
    StoryFrameReaction,
    StoryFrameReplySnapshot,
    StoryFrameType,
    StoryFrameView,
)


def test_story_defaults_audience_to_all_paired():
    s = Story(id="s1", author_user_id="u1", story_date="2026-05-03")
    assert s.audience_kind is StoryAudience.ALL_PAIRED
    assert s.audience == ()


def test_story_frame_type_enum_round_trip():
    f = StoryFrame(
        id="f1",
        story_id="s1",
        sequence=1,
        frame_type=StoryFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    assert f.frame_type.value == "image"
    assert StoryFrameType("video") is StoryFrameType.VIDEO


def test_story_view_and_reaction_carry_only_keys():
    v = StoryFrameView(frame_id="f1", viewer_user_id="u2")
    r = StoryFrameReaction(frame_id="f1", reactor_user_id="u2", emoji="🔥")
    assert v.frame_id == "f1"
    assert r.emoji == "🔥"


def test_reply_snapshot_is_frozen():
    snap = StoryFrameReplySnapshot(
        thumb_url="/api/media/x.webp",
        author_user_id="u1",
        story_date="2026-05-03",
    )
    assert dataclasses.is_dataclass(snap)
    try:
        snap.thumb_url = "/other"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("StoryFrameReplySnapshot must be frozen")
