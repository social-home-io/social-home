"""Inbound story federation — handlers in :class:`FederationInboundService`.

Covers:
* ``STORY_CREATED`` persists a Story + first frame and republishes
  :class:`StoryFrameAdded` so the realtime layer fans the WS frame.
* ``STORY_FRAME_APPENDED`` lazily creates the parent story if the
  ``STORY_CREATED`` envelope arrived out-of-order.
* ``STORY_FRAME_DELETED`` and ``STORY_DELETED`` flip the bus events.
* Authority mismatch (envelope ``from_instance`` ≠ author's home
  instance) is dropped.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.domain.events import (
    StoryFrameAdded,
    StoryFrameReactionChanged,
    StoryFrameRemoved,
    StoryFrameViewed,
    StoryRemoved,
)
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.user import RemoteUser
from socialhome.repositories import (
    SqliteConversationRepo,
    SqliteSpacePostRepo,
    SqliteSpaceRepo,
    SqliteUserRepo,
)
from socialhome.repositories.story_repo import SqliteStoryRepo
from socialhome.services.federation_inbound_service import FederationInboundService


@pytest.fixture
async def inbound(db, bus):
    user_repo = SqliteUserRepo(db)
    # Seed one local user (so the authority check has something to look up
    # for non-author lookups) and one remote user that maps to peer-a.
    # ``remote_users.instance_id`` FKs ``remote_instances`` so we seed the
    # paired peer first.
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name) VALUES(?,?,?)",
        ("uid-local", "local", "Local"),
    )
    await db.enqueue(
        """INSERT INTO remote_instances(
               id, display_name, remote_identity_pk,
               key_self_to_remote, key_remote_to_self,
               remote_inbox_url, local_inbox_id
           ) VALUES(?,?,?,?,?,?,?)""",
        ("peer-a", "Peer A", "00" * 32, "k1", "k2", "https://peer-a/wh", "wh-a"),
    )
    await user_repo.upsert_remote(
        RemoteUser(
            user_id="uid-remote",
            instance_id="peer-a",
            remote_username="alice",
            display_name="Alice",
        ),
    )
    return FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=user_repo,
        story_repo=SqliteStoryRepo(db),
    )


def _event(event_type, payload, *, from_instance="peer-a"):
    return FederationEvent(
        msg_id="msg-" + event_type.value,
        event_type=event_type,
        from_instance=from_instance,
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
    )


def _create_payload(**over):
    base = {
        "story_id": "s-fed-1",
        "frame_id": "f-fed-1",
        "author_user_id": "uid-remote",
        "story_date": "2026-05-05",
        "sequence": 1,
        "audience_kind": "all_paired",
        "audience": [],
        "frame_type": "image",
        "media_url": "/api/media/x.webp",
        "caption_text": None,
        "caption_emoji": None,
        "duration_ms": None,
        "expires_at": "2026-06-04T00:00:00Z",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    base.update(over)
    return base


# ─── STORY_CREATED ────────────────────────────────────────────────────────


async def test_story_created_persists_story_and_frame(db, bus, inbound):
    captured: list[StoryFrameAdded] = []
    bus.subscribe(StoryFrameAdded, captured.append)

    await inbound._on_story_created(
        _event(FederationEventType.STORY_CREATED, _create_payload()),
    )

    story = await inbound._story_repo.get_story("s-fed-1")
    assert story is not None
    assert story.author_user_id == "uid-remote"
    frames = await inbound._story_repo.list_frames("s-fed-1")
    assert len(frames) == 1 and frames[0].id == "f-fed-1"
    assert len(captured) == 1
    assert captured[0].is_first_frame is True
    assert captured[0].story_id == "s-fed-1"


async def test_story_created_authority_mismatch_dropped(db, bus, inbound):
    """Envelope claims author-home != envelope sender → drop."""
    captured: list[StoryFrameAdded] = []
    bus.subscribe(StoryFrameAdded, captured.append)

    # Author 'uid-remote' lives on peer-a; envelope arrives from peer-b → mismatch
    await inbound._on_story_created(
        _event(
            FederationEventType.STORY_CREATED,
            _create_payload(),
            from_instance="peer-b",
        ),
    )
    assert await inbound._story_repo.get_story("s-fed-1") is None
    assert captured == []


async def test_story_frame_appended_creates_parent_if_missing(db, bus, inbound):
    """Out-of-order delivery: FRAME_APPENDED arrives before CREATED."""
    captured: list[StoryFrameAdded] = []
    bus.subscribe(StoryFrameAdded, captured.append)

    await inbound._on_story_frame_appended(
        _event(
            FederationEventType.STORY_FRAME_APPENDED,
            _create_payload(frame_id="f-fed-2", sequence=2),
        ),
    )
    story = await inbound._story_repo.get_story("s-fed-1")
    assert story is not None
    frames = await inbound._story_repo.list_frames("s-fed-1")
    assert [f.id for f in frames] == ["f-fed-2"]
    assert len(captured) == 1 and captured[0].is_first_frame is False


async def test_story_frame_appended_to_existing_story(db, bus, inbound):
    # Land STORY_CREATED first
    await inbound._on_story_created(
        _event(FederationEventType.STORY_CREATED, _create_payload()),
    )
    captured: list[StoryFrameAdded] = []
    bus.subscribe(StoryFrameAdded, captured.append)

    await inbound._on_story_frame_appended(
        _event(
            FederationEventType.STORY_FRAME_APPENDED,
            _create_payload(frame_id="f-fed-2", sequence=2),
        ),
    )
    frames = await inbound._story_repo.list_frames("s-fed-1")
    assert {f.id for f in frames} == {"f-fed-1", "f-fed-2"}
    assert len(captured) == 1
    assert captured[0].frame_id == "f-fed-2"
    assert captured[0].is_first_frame is False


async def test_story_frame_deleted_removes_and_publishes(db, bus, inbound):
    await inbound._on_story_created(
        _event(FederationEventType.STORY_CREATED, _create_payload()),
    )
    captured: list[StoryFrameRemoved] = []
    bus.subscribe(StoryFrameRemoved, captured.append)

    await inbound._on_story_frame_deleted(
        _event(
            FederationEventType.STORY_FRAME_DELETED,
            {"story_id": "s-fed-1", "frame_id": "f-fed-1"},
        ),
    )
    assert await inbound._story_repo.get_frame("f-fed-1") is None
    assert len(captured) == 1


async def test_story_deleted_removes_story_and_publishes(db, bus, inbound):
    await inbound._on_story_created(
        _event(FederationEventType.STORY_CREATED, _create_payload()),
    )
    captured: list[StoryRemoved] = []
    bus.subscribe(StoryRemoved, captured.append)

    await inbound._on_story_deleted(
        _event(
            FederationEventType.STORY_DELETED,
            {"story_id": "s-fed-1", "author_user_id": "uid-remote"},
        ),
    )
    assert await inbound._story_repo.get_story("s-fed-1") is None
    assert len(captured) == 1


async def test_handlers_registered_when_story_repo_present(db, bus):
    """attach_to() registers the 4 STORY_* handlers when the repo is wired."""
    user_repo = SqliteUserRepo(db)
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=user_repo,
        story_repo=SqliteStoryRepo(db),
    )
    fake_fed = type("F", (), {})()
    fake_fed._event_registry = type(
        "R",
        (),
        {
            "_handlers": {},
            "register": lambda self, t, h: self._handlers.__setitem__(t, h),
        },
    )()
    svc.attach_to(fake_fed)
    registered = set(fake_fed._event_registry._handlers.keys())
    assert FederationEventType.STORY_CREATED in registered
    assert FederationEventType.STORY_FRAME_APPENDED in registered
    assert FederationEventType.STORY_FRAME_DELETED in registered
    assert FederationEventType.STORY_DELETED in registered
    assert FederationEventType.STORY_FRAME_VIEWED in registered
    assert FederationEventType.STORY_FRAME_REACTED in registered
    assert FederationEventType.STORY_FRAME_REACTION_REMOVED in registered


async def test_story_frame_viewed_persists_and_publishes(db, bus, inbound):
    """A remote viewer's view receipt lands in story_frame_views and
    fires StoryFrameViewed so realtime can ping the author."""
    # Seed the parent story + frame via the CREATED handler.
    await inbound._on_story_created(
        _event(FederationEventType.STORY_CREATED, _create_payload()),
    )
    captured: list[StoryFrameViewed] = []
    bus.subscribe(StoryFrameViewed, captured.append)

    await inbound._on_story_frame_viewed(
        _event(
            FederationEventType.STORY_FRAME_VIEWED,
            {
                "story_id": "s-fed-1",
                "frame_id": "f-fed-1",
                # ``uid-remote`` lives on peer-a (seeded in the fixture);
                # so does the envelope from_instance — authority matches.
                "viewer_user_id": "uid-remote",
                "author_user_id": "uid-local",
            },
        ),
    )
    views = await inbound._story_repo.list_views_for_frame("f-fed-1")
    assert len(views) == 1 and views[0].viewer_user_id == "uid-remote"
    assert len(captured) == 1
    assert captured[0].viewer_user_id == "uid-remote"


async def test_story_frame_viewed_authority_mismatch_dropped(db, bus, inbound):
    """The viewer must live on the envelope's signed sender."""
    await inbound._on_story_created(
        _event(FederationEventType.STORY_CREATED, _create_payload()),
    )
    captured: list[StoryFrameViewed] = []
    bus.subscribe(StoryFrameViewed, captured.append)

    await inbound._on_story_frame_viewed(
        _event(
            FederationEventType.STORY_FRAME_VIEWED,
            {
                "story_id": "s-fed-1",
                "frame_id": "f-fed-1",
                "viewer_user_id": "uid-remote",  # lives on peer-a
                "author_user_id": "uid-local",
            },
            from_instance="peer-b",  # mismatch
        ),
    )
    assert await inbound._story_repo.list_views_for_frame("f-fed-1") == []
    assert captured == []


async def test_story_frame_reacted_persists_and_publishes(db, bus, inbound):
    await inbound._on_story_created(
        _event(FederationEventType.STORY_CREATED, _create_payload()),
    )
    captured: list[StoryFrameReactionChanged] = []
    bus.subscribe(StoryFrameReactionChanged, captured.append)

    await inbound._on_story_frame_reacted(
        _event(
            FederationEventType.STORY_FRAME_REACTED,
            {
                "story_id": "s-fed-1",
                "frame_id": "f-fed-1",
                "reactor_user_id": "uid-remote",
                "author_user_id": "uid-local",
                "emoji": "🔥",
            },
        ),
    )
    rs = await inbound._story_repo.list_reactions_for_frame("f-fed-1")
    assert len(rs) == 1 and rs[0].emoji == "🔥"
    assert len(captured) == 1 and captured[0].emoji == "🔥"


async def test_story_frame_reaction_removed_clears(db, bus, inbound):
    """REACTION_REMOVED clears the row and publishes ``emoji=None``."""
    await inbound._on_story_created(
        _event(FederationEventType.STORY_CREATED, _create_payload()),
    )
    # Seed a reaction first.
    await inbound._on_story_frame_reacted(
        _event(
            FederationEventType.STORY_FRAME_REACTED,
            {
                "story_id": "s-fed-1",
                "frame_id": "f-fed-1",
                "reactor_user_id": "uid-remote",
                "author_user_id": "uid-local",
                "emoji": "🔥",
            },
        ),
    )
    captured: list[StoryFrameReactionChanged] = []
    bus.subscribe(StoryFrameReactionChanged, captured.append)

    await inbound._on_story_frame_reaction_removed(
        _event(
            FederationEventType.STORY_FRAME_REACTION_REMOVED,
            {
                "story_id": "s-fed-1",
                "frame_id": "f-fed-1",
                "reactor_user_id": "uid-remote",
                "author_user_id": "uid-local",
            },
        ),
    )
    assert await inbound._story_repo.list_reactions_for_frame("f-fed-1") == []
    assert len(captured) == 1 and captured[0].emoji is None


async def test_handlers_skipped_when_story_repo_missing(db, bus):
    """Tests that don't pass a story_repo don't see STORY_* registered."""
    user_repo = SqliteUserRepo(db)
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=user_repo,
    )
    fake_fed = type("F", (), {})()
    fake_fed._event_registry = type(
        "R",
        (),
        {
            "_handlers": {},
            "register": lambda self, t, h: self._handlers.__setitem__(t, h),
        },
    )()
    svc.attach_to(fake_fed)
    registered = set(fake_fed._event_registry._handlers.keys())
    assert FederationEventType.STORY_CREATED not in registered
