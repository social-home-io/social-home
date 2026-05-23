"""Tests for the SpacePostCreated → SPACE_POST_CREATED federation bridge."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from socialhome.domain.events import (
    PostDeleted,
    PostEdited,
    SpacePostCreated,
)
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


async def test_inbound_replay_does_not_loop_back_via_outbound():
    """When ``federation_inbound_service`` receives a SPACE_POST_CREATED
    from peer P and re-publishes ``SpacePostCreated`` to the local
    bus (so realtime / search / HA bridge see the new row), the
    outbound bridge MUST NOT re-broadcast — otherwise we get a
    federation loop. The ``origin_instance_id`` field on the bus
    event is the gate: ``None`` = local origination, set =
    inbound replay → skip."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    SpacePostOutbound(bus=bus, federation_service=federation)

    post = Post(
        id="p-from-peer",
        author="uid-pascal",
        type=PostType.TEXT,
        content="hi",
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(
        SpacePostCreated(
            post=post,
            space_id="sp-1",
            origin_instance_id="peer-pascal-instance",
        )
    )
    federation.broadcast_to_space_members.assert_not_awaited()


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


# ─── PostEdited / PostDeleted federation (PR #431) ─────────────────────


async def test_post_edited_in_space_broadcasts_update():
    """When a local user edits a space post, the bus event must
    federate via ``SPACE_POST_UPDATED`` so remote members see the new
    body. PR #431 plumbed ``space_id`` onto :class:`PostEdited` for
    exactly this — the outbound bridge gates on it."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    SpacePostOutbound(bus=bus, federation_service=federation)

    post = Post(
        id="post-edit-1",
        author="uid-alice",
        type=PostType.TEXT,
        content="updated body",
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(PostEdited(post=post, space_id="sp-1"))
    federation.broadcast_to_space_members.assert_awaited_once()
    call = federation.broadcast_to_space_members.call_args
    assert call.args[0] == "sp-1"
    assert call.args[1] is FederationEventType.SPACE_POST_UPDATED
    payload = call.args[2]
    assert payload["post_id"] == "post-edit-1"
    assert payload["content"] == "updated body"


async def test_post_edited_household_only_skipped():
    """``PostEdited`` with no space_id is a household-feed edit — must
    not federate as a space update."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    SpacePostOutbound(bus=bus, federation_service=federation)

    post = Post(
        id="p",
        author="u",
        type=PostType.TEXT,
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(PostEdited(post=post))
    federation.broadcast_to_space_members.assert_not_awaited()


async def test_post_edited_inbound_replay_does_not_loop():
    """Symmetric to the create case — an inbound SPACE_POST_UPDATED
    replays as ``PostEdited`` with ``origin_instance_id`` set; the
    outbound MUST NOT re-broadcast."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    SpacePostOutbound(bus=bus, federation_service=federation)

    post = Post(
        id="p",
        author="u",
        type=PostType.TEXT,
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(
        PostEdited(post=post, space_id="sp-1", origin_instance_id="peer-x"),
    )
    federation.broadcast_to_space_members.assert_not_awaited()


async def test_post_edited_broadcast_failure_swallowed():
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock(
        side_effect=RuntimeError("transport down"),
    )
    SpacePostOutbound(bus=bus, federation_service=federation)
    post = Post(
        id="p",
        author="u",
        type=PostType.TEXT,
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(PostEdited(post=post, space_id="sp-1"))


async def test_post_deleted_in_space_broadcasts_delete():
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    SpacePostOutbound(bus=bus, federation_service=federation)

    await bus.publish(PostDeleted(post_id="post-del-1", space_id="sp-1"))
    federation.broadcast_to_space_members.assert_awaited_once()
    call = federation.broadcast_to_space_members.call_args
    assert call.args[1] is FederationEventType.SPACE_POST_DELETED
    payload = call.args[2]
    assert payload["post_id"] == "post-del-1"
    assert payload["space_id"] == "sp-1"


async def test_post_deleted_household_only_skipped():
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    SpacePostOutbound(bus=bus, federation_service=federation)
    await bus.publish(PostDeleted(post_id="p"))
    federation.broadcast_to_space_members.assert_not_awaited()


async def test_post_deleted_inbound_replay_does_not_loop():
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    SpacePostOutbound(bus=bus, federation_service=federation)
    await bus.publish(
        PostDeleted(post_id="p", space_id="sp-1", origin_instance_id="peer-x"),
    )
    federation.broadcast_to_space_members.assert_not_awaited()


async def test_post_deleted_broadcast_failure_swallowed():
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock(
        side_effect=RuntimeError("transport down"),
    )
    SpacePostOutbound(bus=bus, federation_service=federation)
    await bus.publish(PostDeleted(post_id="p", space_id="sp-1"))


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


# ─── SPACE_MEDIA_BLOB — outbox-driven bytes federation ────────────────


async def test_space_post_created_enqueues_outbox_per_peer_per_blob():
    """After SPACE_POST_CREATED broadcasts, the outbound enqueues one
    media-outbox row per (peer, blob) tuple. The scheduler reads
    these lazily, chunks the file, and ships SPACE_MEDIA_BLOB events
    over the federation outbox. Without this, the receiver's
    ``<img src>`` 404s because the bytes never federate."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    federation._own_instance_id = "self-id"
    federation_repo = AsyncMock()
    federation_repo.list_member_instance_ids = AsyncMock(
        return_value=["peer-a", "peer-b", "self-id"],  # self filtered out
    )
    media_sync = AsyncMock()
    media_sync.enqueue_for_post = AsyncMock()
    SpacePostOutbound(
        bus=bus,
        federation_service=federation,
        media_sync=media_sync,
        federation_repo=federation_repo,
    )
    post = Post(
        id="p-mediated",
        author="u",
        type=PostType.IMAGE,
        image_urls=("api/media/img-a.webp", "api/media/img-b.webp"),
        media_url="api/media/cover.webp",
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))

    media_sync.enqueue_for_post.assert_awaited_once()
    call = media_sync.enqueue_for_post.call_args
    assert call.kwargs["post_id"] == "p-mediated"
    # Self instance dropped from the target list.
    assert set(call.kwargs["target_instance_ids"]) == {"peer-a", "peer-b"}
    # All referenced media URLs enqueued.
    assert set(call.kwargs["media_urls"]) == {
        "api/media/img-a.webp",
        "api/media/img-b.webp",
        "api/media/cover.webp",
    }


async def test_space_post_created_no_media_skips_enqueue():
    """Text-only post — no media URLs → no media-outbox enqueue."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    federation._own_instance_id = "self-id"
    media_sync = AsyncMock()
    media_sync.enqueue_for_post = AsyncMock()
    SpacePostOutbound(
        bus=bus,
        federation_service=federation,
        media_sync=media_sync,
        federation_repo=AsyncMock(),
    )
    post = Post(
        id="p-text",
        author="u",
        type=PostType.TEXT,
        content="hi",
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))
    media_sync.enqueue_for_post.assert_not_awaited()


async def test_space_post_created_no_media_sync_wired_is_noop():
    """SpacePostOutbound without ``media_sync`` (test stacks) doesn't
    crash on media posts — just skips the bytes federation."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    # NO media_sync / federation_repo.
    SpacePostOutbound(bus=bus, federation_service=federation)
    post = Post(
        id="p",
        author="u",
        type=PostType.IMAGE,
        image_urls=("api/media/whatever.webp",),
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    # Should not raise.
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))
