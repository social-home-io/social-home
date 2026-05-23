"""Tests for the SpacePostCreated → SPACE_POST_CREATED federation bridge."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from socialhome.domain.events import SpacePostCreated
from socialhome.domain.federation import FederationEventType
from socialhome.domain.post import Post, PostType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.space_post_outbound import SpacePostOutbound


async def test_space_post_created_broadcasts_to_space_members():
    """When a local user creates a space post, the bus event must
    federate via ``broadcast_to_space_members`` so every member
    household (direct + mesh-only) sees it. Without this bridge,
    posts in cross-household spaces stay invisible to remote
    members."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    SpacePostOutbound(bus=bus, federation_service=federation)

    post = Post(
        id="post-xyz",
        author="uid-alice",
        type=PostType.TEXT,
        content="hello space members",
        created_at=datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc),
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))

    federation.broadcast_to_space_members.assert_awaited_once()
    call = federation.broadcast_to_space_members.call_args
    assert call.args[0] == "sp-1"
    assert call.args[1] is FederationEventType.SPACE_POST_CREATED
    payload = call.args[2]
    assert payload["id"] == "post-xyz"
    assert payload["space_id"] == "sp-1"
    assert payload["author"] == "uid-alice"
    assert payload["type"] == "text"
    assert payload["content"] == "hello space members"
    # ``broadcast_to_space_members`` already targets members-only
    # via space_instances and routes mesh-fallback for unpaired
    # members — see CLAUDE.md "Encryption-First Rule". The bridge
    # doesn't need to gate; it just publishes.


async def test_household_post_does_not_federate_via_space_bridge():
    """SpacePostCreated CAN fire with empty space_id for some legacy
    paths; the bridge must skip those to avoid mis-routing a
    household post as space content."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    SpacePostOutbound(bus=bus, federation_service=federation)

    post = Post(
        id="p",
        author="uid-alice",
        type=PostType.TEXT,
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(SpacePostCreated(post=post, space_id=""))
    federation.broadcast_to_space_members.assert_not_awaited()


async def test_broadcast_failure_logged_but_swallowed():
    """A federation failure during broadcast must NOT propagate back
    to the bus — the local DB write already happened and we don't
    want the realtime/HA/search subscribers to receive a partial
    failure exception. We log + drop."""

    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock(
        side_effect=RuntimeError("transport down")
    )
    SpacePostOutbound(bus=bus, federation_service=federation)

    post = Post(
        id="p",
        author="u",
        type=PostType.TEXT,
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    # Should not raise.
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))
    federation.broadcast_to_space_members.assert_awaited_once()


@pytest.mark.parametrize(
    "type_,extra",
    [
        (PostType.IMAGE, {"image_urls": ("/api/media/a.webp", "/api/media/b.webp")}),
        (PostType.VIDEO, {"media_url": "/api/media/clip.webm"}),
    ],
)
async def test_payload_carries_media_fields(type_, extra):
    """The receiver's ``_post_from_payload`` reads ``media_url`` and
    ``image_urls`` — the outbound bridge must include them so
    rendered posts on remote members show the same media as the
    host's local card."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    SpacePostOutbound(bus=bus, federation_service=federation)

    post = Post(
        id="p",
        author="u",
        type=type_,
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        **extra,
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))
    payload = federation.broadcast_to_space_members.call_args.args[2]
    if "image_urls" in extra:
        assert payload["image_urls"] == list(extra["image_urls"])
    if "media_url" in extra:
        assert payload["media_url"] == extra["media_url"]
