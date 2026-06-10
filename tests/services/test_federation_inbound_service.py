"""Tests for :class:`FederationInboundService` — §24 inbound event dispatch."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from socialhome.domain.events import (
    CommentAdded,
    DmMessageCreated,
    PostDeleted,
    SpaceConfigChanged,
    SpacePostCreated,
    UserStatusChanged,
)
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.post import Post, PostType
from socialhome.domain.space import JoinMode, SpaceMember, SpaceType
from socialhome.repositories import (
    SqliteConversationRepo,
    SqliteSpacePostRepo,
    SqliteSpaceRepo,
    SqliteUserRepo,
)
from socialhome.repositories.dm_routing_repo import SqliteDmRoutingRepo
from socialhome.services.federation_inbound_service import (
    FederationInboundService,
)


@pytest.fixture
async def inbound(db, bus):
    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    return service


def _event(
    event_type, payload, *, from_instance="peer-a", space_id=None, media_bytes=None
):
    return FederationEvent(
        msg_id="msg-" + event_type.value,
        event_type=event_type,
        from_instance=from_instance,
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
        space_id=space_id,
        media_bytes=media_bytes,
    )


# ─── DM ──────────────────────────────────────────────────────────────────


async def test_dm_message_persists_and_publishes_event(db, bus, inbound):
    # Seed conversation row so FK is satisfied
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?, datetime('now'))",
        ("conv-1", "dm"),
    )
    captured: list[DmMessageCreated] = []
    bus.subscribe(DmMessageCreated, captured.append)

    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {
                "conversation_id": "conv-1",
                "message_id": "m-1",
                "sender_user_id": "user-remote",
                "sender_display_name": "Alice",
                "content": "hi",
                "recipient_user_ids": ["user-local"],
            },
        )
    )

    row = await db.fetchone(
        "SELECT id, content FROM conversation_messages WHERE id=?",
        ("m-1",),
    )
    assert row is not None
    assert row["content"] == "hi"
    assert len(captured) == 1
    assert captured[0].conversation_id == "conv-1"
    assert captured[0].recipient_user_ids == ("user-local",)


async def test_dm_message_duplicate_transport_does_not_publish_created_twice(
    db,
    bus,
    inbound,
):
    """Second arrival of the same envelope (e.g. WebRTC delivers, then
    HTTPS-inbox redelivers) must NOT republish ``DmMessageCreated`` —
    otherwise the user gets two bell rows + two pushes for one
    message. Race-safety is at the repo layer; this test pins the
    end-to-end contract that the inbound handler honours it."""
    from socialhome.domain.events import DmMessageUpdated

    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?, datetime('now'))",
        ("conv-dup", "dm"),
    )
    created: list[DmMessageCreated] = []
    updated: list[DmMessageUpdated] = []
    bus.subscribe(DmMessageCreated, created.append)
    bus.subscribe(DmMessageUpdated, updated.append)

    payload = {
        "conversation_id": "conv-dup",
        "message_id": "m-dup",
        "sender_user_id": "user-remote",
        "sender_display_name": "Alice",
        "content": "hi",
        "recipient_user_ids": ["user-local"],
    }
    await inbound._on_dm_message(_event(FederationEventType.DM_MESSAGE, payload))
    await inbound._on_dm_message(_event(FederationEventType.DM_MESSAGE, payload))

    # First arrival fired Created; second arrival is a silent no-op
    # (no Created, and no Updated either — there was no real edit).
    assert len(created) == 1
    assert len(updated) == 0


async def test_dm_message_edit_replay_publishes_updated(db, bus, inbound):
    """A genuine edit (payload carries ``edited_at``) on an existing
    row still publishes ``DmMessageUpdated`` so the SPA can patch
    the bubble in place. The audit pass that suppresses the no-op
    updated-publish for duplicate-transport replays must not also
    suppress legitimate edits."""
    from socialhome.domain.events import DmMessageUpdated

    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?, datetime('now'))",
        ("conv-edit", "dm"),
    )
    created: list[DmMessageCreated] = []
    updated: list[DmMessageUpdated] = []
    bus.subscribe(DmMessageCreated, created.append)
    bus.subscribe(DmMessageUpdated, updated.append)

    # First delivery — brand-new message.
    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {
                "conversation_id": "conv-edit",
                "message_id": "m-edit",
                "sender_user_id": "user-remote",
                "sender_display_name": "Alice",
                "content": "original",
                "recipient_user_ids": ["user-local"],
            },
        )
    )
    # Sender re-fans the envelope with edited content + an
    # ``edited_at`` timestamp — the signal that this is a real edit.
    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {
                "conversation_id": "conv-edit",
                "message_id": "m-edit",
                "sender_user_id": "user-remote",
                "sender_display_name": "Alice",
                "content": "edited content",
                "recipient_user_ids": ["user-local"],
                "edited_at": "2026-05-21T12:00:00+00:00",
            },
        )
    )

    assert len(created) == 1
    assert len(updated) == 1
    assert updated[0].content == "edited content"
    row = await db.fetchone(
        "SELECT content FROM conversation_messages WHERE id=?",
        ("m-edit",),
    )
    assert row is not None
    assert row["content"] == "edited content"


async def test_dm_message_missing_fields_drops(inbound):
    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {"conversation_id": "conv-1"},  # missing message_id/sender_user_id
        )
    )
    # Nothing raised, nothing persisted — test passes when no exception


async def test_dm_message_lazy_creates_conversation_before_seq_record(db, bus):
    """Regression: DM_MESSAGE for an unknown conversation must not trip
    a FOREIGN KEY constraint on ``conversation_sender_sequences``.

    Pre-fix, ``_on_dm_message`` called ``record_received_seq`` (which
    FK-references ``conversations(id)``) *before* the
    ``_ensure_remote_dm_conversation`` lazy-create that builds the
    conversation row on the receiver. On a cross-household first send
    the receiver had no row yet → ``sqlite3.IntegrityError: FOREIGN
    KEY constraint failed`` → the handler crashed → the message never
    landed in ``/api/conversations``. Federation-demo's ``verify`` step
    surfaced this on the a→c DM check.

    After the fix: the conversation row is upserted up-front so the
    sequence record always finds its FK target.
    """
    routing_repo = SqliteDmRoutingRepo(db)
    inbound = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        dm_routing_repo=routing_repo,
    )

    # Deliberately do NOT pre-seed the conversation row. The handler
    # must lazy-create it.
    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {
                "conversation_id": "conv-fresh",
                "message_id": "m-1",
                "sender_user_id": "user-remote",
                "content": "first contact",
                # Seq must be carried so the sequence-record path
                # (which is where the FK trips) actually runs.
                "sender_seq": 1,
                "recipient_user_ids": ["user-local"],
            },
        ),
    )

    # The conversation row was created on the receiver.
    conv_rows = await db.fetchall(
        "SELECT id FROM conversations WHERE id = ?",
        ("conv-fresh",),
    )
    assert conv_rows, "conversation row should be lazy-created by _on_dm_message"

    # The sequence row landed (no FK violation; watermark = 1).
    assert (
        await routing_repo.peek_sender_seq(
            conversation_id="conv-fresh",
            sender_user_id="user-remote",
        )
        == 1
    )


async def test_dm_message_first_inbound_sweeps_bogus_gaps(db, bus):
    """End-to-end regression for the receiver-side high-watermark fix.

    Pre-fix, the gap detector never advanced its high-watermark on the
    inbound path, so every message after the first re-tripped a bogus
    ``missing=1..N-1`` gap that landed in ``conversation_message_gaps``.
    The SPA's DmThreadPage renders those rows as a "X messages may be
    missing" banner.

    Repro shape:
      * pre-existing bogus gap rows for the (conv, sender) pair
      * ``conversation_sender_sequences`` row is empty (watermark = 0)
      * inbound ``DM_MESSAGE`` arrives with ``sender_seq=5``

    After the fix: gap rows are swept, watermark is seeded to 5, no new
    gap is inserted.
    """
    routing_repo = SqliteDmRoutingRepo(db)
    inbound = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        dm_routing_repo=routing_repo,
    )
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?, datetime('now'))",
        ("conv-1", "dm"),
    )
    # Pre-seed the bogus rows the pre-fix detector would have inserted.
    await routing_repo.insert_gaps(
        conversation_id="conv-1",
        sender_user_id="user-remote",
        expected_seqs=[1, 2, 3, 4],
    )
    assert len(await routing_repo.list_open_gaps("conv-1")) == 4

    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {
                "conversation_id": "conv-1",
                "message_id": "m-5",
                "sender_user_id": "user-remote",
                "content": "hi",
                "sender_seq": 5,
            },
        ),
    )

    # All bogus rows for (conv-1, user-remote) are gone.
    assert await routing_repo.list_open_gaps("conv-1") == []
    # Watermark seeded to the first observed seq.
    assert (
        await routing_repo.peek_sender_seq(
            conversation_id="conv-1",
            sender_user_id="user-remote",
        )
        == 5
    )

    # Follow-up message at the next seq — must NOT re-trip any gap.
    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {
                "conversation_id": "conv-1",
                "message_id": "m-6",
                "sender_user_id": "user-remote",
                "content": "next",
                "sender_seq": 6,
            },
        ),
    )
    assert await routing_repo.list_open_gaps("conv-1") == []
    assert (
        await routing_repo.peek_sender_seq(
            conversation_id="conv-1",
            sender_user_id="user-remote",
        )
        == 6
    )


async def test_dm_message_first_inbound_does_not_disturb_other_senders_gaps(db, bus):
    """The bogus-gap sweep is scoped to (conv, sender). Other senders'
    legitimately-recorded gap rows in the same conversation survive."""
    routing_repo = SqliteDmRoutingRepo(db)
    inbound = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        dm_routing_repo=routing_repo,
    )
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?, datetime('now'))",
        ("conv-1", "dm"),
    )
    # Two senders, both with rows in conversation_message_gaps.
    await routing_repo.insert_gaps(
        conversation_id="conv-1",
        sender_user_id="user-alice",
        expected_seqs=[1, 2],
    )
    await routing_repo.insert_gaps(
        conversation_id="conv-1",
        sender_user_id="user-bob",
        expected_seqs=[3],
    )

    # First inbound from alice triggers the sweep — only her rows go.
    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {
                "conversation_id": "conv-1",
                "message_id": "m-3",
                "sender_user_id": "user-alice",
                "content": "hi",
                "sender_seq": 3,
            },
        ),
    )
    remaining = await routing_repo.list_open_gaps("conv-1")
    assert [(g["sender_user_id"], g["expected_seq"]) for g in remaining] == [
        ("user-bob", 3),
    ]


async def test_dm_message_deleted_soft_deletes(db, bus, inbound):
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?, datetime('now'))",
        ("conv-1", "dm"),
    )
    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {
                "conversation_id": "conv-1",
                "message_id": "m-1",
                "sender_user_id": "user-remote",
                "content": "hi",
            },
        )
    )
    await inbound._on_dm_deleted(
        _event(
            FederationEventType.DM_MESSAGE_DELETED,
            {"message_id": "m-1"},
        )
    )
    row = await db.fetchone(
        "SELECT deleted FROM conversation_messages WHERE id=?",
        ("m-1",),
    )
    assert row["deleted"] == 1


# ─── Space posts ─────────────────────────────────────────────────────────


async def test_space_post_created_persists(db, bus, inbound):
    # Seed the space row — the space_post_repo has no FK back to spaces so
    # a minimal insert suffices for the v1 schema.
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "sp-1",
            "Space 1",
            "peer-a",
            "owner",
            "aa" * 32,
            SpaceType.HOUSEHOLD.value,
            JoinMode.INVITE_ONLY.value,
        ),
    )
    captured: list[SpacePostCreated] = []
    bus.subscribe(SpacePostCreated, captured.append)

    await inbound._on_space_post_created(
        _event(
            FederationEventType.SPACE_POST_CREATED,
            {
                "id": "post-1",
                "author": "user-remote",
                "type": "text",
                "content": "hello",
            },
            space_id="sp-1",
        )
    )
    row = await db.fetchone("SELECT id FROM space_posts WHERE id=?", ("post-1",))
    assert row is not None
    assert len(captured) == 1
    assert captured[0].post.id == "post-1"
    assert captured[0].space_id == "sp-1"


async def test_space_post_created_carries_location(db, bus, inbound):
    """Location posts ride on SPACE_POST_CREATED with a `location` block
    in the payload. Inbound decodes it into a LocationData."""
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "sp-loc",
            "Space",
            "peer-a",
            "owner",
            "aa" * 32,
            SpaceType.HOUSEHOLD.value,
            JoinMode.INVITE_ONLY.value,
        ),
    )
    captured: list[SpacePostCreated] = []
    bus.subscribe(SpacePostCreated, captured.append)

    await inbound._on_space_post_created(
        _event(
            FederationEventType.SPACE_POST_CREATED,
            {
                "id": "post-loc",
                "author": "user-remote",
                "type": "location",
                "content": "Sunset",
                "location": {"lat": 52.5200, "lon": 4.0600, "label": "Marina"},
            },
            space_id="sp-loc",
        )
    )
    assert captured and captured[0].post.location is not None
    loc = captured[0].post.location
    assert loc.lat == 52.5200
    assert loc.lon == 4.0600
    assert loc.label == "Marina"


async def test_space_post_created_truncates_full_precision_gps(db, bus, inbound):
    """§25 / CLAUDE.md GPS rule: a peer can put full precision on the
    wire, but the receiver MUST truncate to 4 decimal places before
    persisting / publishing — same invariant as outbound posts. The
    persisted Post.location lat/lon should match
    ``round(raw, 4)`` exactly."""
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "sp-trunc",
            "Space",
            "peer-a",
            "owner",
            "aa" * 32,
            SpaceType.HOUSEHOLD.value,
            JoinMode.INVITE_ONLY.value,
        ),
    )
    captured: list[SpacePostCreated] = []
    bus.subscribe(SpacePostCreated, captured.append)

    await inbound._on_space_post_created(
        _event(
            FederationEventType.SPACE_POST_CREATED,
            {
                "id": "post-trunc",
                "author": "user-remote",
                "type": "location",
                "location": {
                    "lat": 52.523456789,
                    "lon": 4.067123456,
                    "label": "Park",
                },
            },
            space_id="sp-trunc",
        )
    )
    assert captured and captured[0].post.location is not None
    loc = captured[0].post.location
    assert loc.lat == 52.5235
    assert loc.lon == 4.0671
    # Defence in depth: re-rounding the persisted value MUST be a no-op
    # (i.e. no extra decimals snuck through).
    assert round(loc.lat, 4) == loc.lat
    assert round(loc.lon, 4) == loc.lon


async def test_space_post_created_drops_malformed_location(db, bus, inbound):
    """A peer that sends a non-numeric lat shouldn't break the post —
    the location is silently dropped, the rest is preserved."""
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "sp-loc-bad",
            "Space",
            "peer-a",
            "owner",
            "aa" * 32,
            SpaceType.HOUSEHOLD.value,
            JoinMode.INVITE_ONLY.value,
        ),
    )
    captured: list[SpacePostCreated] = []
    bus.subscribe(SpacePostCreated, captured.append)

    await inbound._on_space_post_created(
        _event(
            FederationEventType.SPACE_POST_CREATED,
            {
                "id": "post-loc-bad",
                "author": "user-remote",
                "type": "location",
                "location": {"lat": "not-a-number", "lon": 4.0600},
            },
            space_id="sp-loc-bad",
        )
    )
    assert captured
    assert captured[0].post.location is None


# ─── User status ─────────────────────────────────────────────────────────


async def test_user_status_updated_publishes_bus_event(bus, inbound):
    captured: list[UserStatusChanged] = []
    bus.subscribe(UserStatusChanged, captured.append)

    await inbound._on_user_status_updated(
        _event(
            FederationEventType.USER_STATUS_UPDATED,
            {"user_id": "u-1", "emoji": "🌴", "text": "On leave"},
        )
    )
    assert len(captured) == 1
    assert captured[0].user_id == "u-1"
    assert captured[0].status is not None
    assert captured[0].status.emoji == "🌴"


async def test_user_status_cleared_publishes_none(bus, inbound):
    captured: list[UserStatusChanged] = []
    bus.subscribe(UserStatusChanged, captured.append)

    await inbound._on_user_status_updated(
        _event(
            FederationEventType.USER_STATUS_UPDATED,
            {"user_id": "u-1", "status_cleared": True},
        )
    )
    assert captured[0].status is None


# ─── Remote users ────────────────────────────────────────────────────────


async def test_dm_reaction_add_and_remove(db, bus, inbound):
    """DM_MESSAGE_REACTION handles both action=add and action=remove."""
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?, datetime('now'))",
        ("conv-1", "dm"),
    )
    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {
                "conversation_id": "conv-1",
                "message_id": "m-1",
                "sender_user_id": "user-remote",
                "content": "hi",
            },
        )
    )
    await inbound._on_dm_reaction(
        _event(
            FederationEventType.DM_MESSAGE_REACTION,
            {"message_id": "m-1", "user_id": "user-x", "emoji": "👍", "action": "add"},
        )
    )
    rows = await db.fetchall(
        "SELECT emoji FROM message_reactions WHERE message_id=?",
        ("m-1",),
    )
    assert [r["emoji"] for r in rows] == ["👍"]

    await inbound._on_dm_reaction(
        _event(
            FederationEventType.DM_MESSAGE_REACTION,
            {
                "message_id": "m-1",
                "user_id": "user-x",
                "emoji": "👍",
                "action": "remove",
            },
        )
    )
    rows = await db.fetchall(
        "SELECT emoji FROM message_reactions WHERE message_id=?",
        ("m-1",),
    )
    assert rows == []


async def test_space_post_updated_edits_content(db, bus, inbound):
    # Seed space + post
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "sp-1",
            "Space 1",
            "peer-a",
            "owner",
            "aa" * 32,
            SpaceType.HOUSEHOLD.value,
            JoinMode.INVITE_ONLY.value,
        ),
    )
    repo = SqliteSpacePostRepo(db)
    now = datetime.now(timezone.utc)
    await repo.save(
        "sp-1",
        Post(
            id="p-1",
            author="u",
            type=PostType.TEXT,
            created_at=now,
            content="old content",
        ),
    )

    await inbound._on_space_post_updated(
        _event(
            FederationEventType.SPACE_POST_UPDATED,
            {"id": "p-1", "content": "new content"},
            space_id="sp-1",
        )
    )
    row = await db.fetchone("SELECT content FROM space_posts WHERE id=?", ("p-1",))
    assert row["content"] == "new content"


async def test_space_post_deleted_soft_deletes_and_publishes(db, bus, inbound):
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "sp-1",
            "Space 1",
            "peer-a",
            "owner",
            "aa" * 32,
            SpaceType.HOUSEHOLD.value,
            JoinMode.INVITE_ONLY.value,
        ),
    )
    repo = SqliteSpacePostRepo(db)
    await repo.save(
        "sp-1",
        Post(
            id="p-1",
            author="u",
            type=PostType.TEXT,
            created_at=datetime.now(timezone.utc),
            content="x",
        ),
    )
    captured: list[PostDeleted] = []
    bus.subscribe(PostDeleted, captured.append)

    await inbound._on_space_post_deleted(
        _event(
            FederationEventType.SPACE_POST_DELETED,
            {"post_id": "p-1", "moderated_by": "admin-a"},
            space_id="sp-1",
        )
    )
    row = await db.fetchone("SELECT content FROM space_posts WHERE id=?", ("p-1",))
    assert row["content"] is None
    assert len(captured) == 1


async def _seed_role_space(db, *, owner="peer-a"):
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "sp-role",
            "Space",
            owner,
            "owner",
            "aa" * 32,
            SpaceType.HOUSEHOLD.value,
            JoinMode.INVITE_ONLY.value,
        ),
    )
    await SqliteSpaceRepo(db).save_member(
        SpaceMember(
            space_id="sp-role",
            user_id="u-1",
            role="member",
            joined_at="2026-01-01T00:00:00+00:00",
        )
    )


async def test_space_member_role_changed_promotes_local_member(db, bus, inbound):
    """A host promotion is applied to the local space_members role so admin
    guards + the SPA see it (regression: the event used to be unhandled)."""
    await _seed_role_space(db)
    captured: list = []

    async def _cap(e):
        captured.append(e)

    bus.subscribe(SpaceConfigChanged, _cap)
    await inbound._on_space_member_role_changed(
        _event(
            FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
            {"user_id": "u-1", "role": "admin"},
            space_id="sp-role",
            from_instance="peer-a",  # the owner/host
        )
    )
    row = await db.fetchone(
        "SELECT role FROM space_members WHERE space_id=? AND user_id=?",
        ("sp-role", "u-1"),
    )
    assert row["role"] == "admin"
    # Publishes a local config-changed so connected tabs refresh.
    assert len(captured) == 1


async def test_space_member_role_changed_rejects_non_host(db, inbound):
    """Roles are host-authoritative — a role change from anyone other than
    the owning instance is dropped (no privilege spoofing)."""
    await _seed_role_space(db, owner="peer-a")
    await inbound._on_space_member_role_changed(
        _event(
            FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
            {"user_id": "u-1", "role": "admin"},
            space_id="sp-role",
            from_instance="peer-b",  # NOT the owner
        )
    )
    row = await db.fetchone(
        "SELECT role FROM space_members WHERE space_id=? AND user_id=?",
        ("sp-role", "u-1"),
    )
    assert row["role"] == "member"  # unchanged


async def test_attach_registers_handlers_on_federation_service(db, bus):
    """attach_to wires the expected event types into the dispatcher."""
    registry = MagicMock()
    fake_federation = MagicMock()
    fake_federation._event_registry = registry

    service = FederationInboundService(
        bus=bus,
        conversation_repo=None,  # type: ignore[arg-type]
        space_post_repo=None,  # type: ignore[arg-type]
        space_repo=None,  # type: ignore[arg-type]
        user_repo=None,  # type: ignore[arg-type]
    )
    service.attach_to(fake_federation)

    registered_types = {call.args[0] for call in registry.register.call_args_list}
    assert FederationEventType.DM_MESSAGE in registered_types
    assert FederationEventType.DM_MESSAGE_DELETED in registered_types
    assert FederationEventType.DM_MESSAGE_REACTION in registered_types
    assert FederationEventType.SPACE_POST_CREATED in registered_types
    assert FederationEventType.SPACE_POST_UPDATED in registered_types
    assert FederationEventType.SPACE_POST_DELETED in registered_types
    assert FederationEventType.SPACE_COMMENT_CREATED in registered_types
    assert FederationEventType.SPACE_COMMENT_DELETED in registered_types
    # v_23: SPACE_MEMBER_JOINED / SPACE_MEMBER_LEFT are intentionally NOT
    # registered here — the authority-verifying gossip handler in
    # PrivateSpaceInviteHandler is their sole handler (unsigned roster
    # mutations must never apply).
    assert FederationEventType.SPACE_MEMBER_JOINED not in registered_types
    assert FederationEventType.SPACE_MEMBER_LEFT not in registered_types
    assert FederationEventType.USERS_SYNC in registered_types
    assert FederationEventType.USER_UPDATED in registered_types
    assert FederationEventType.USER_REMOVED in registered_types
    assert FederationEventType.USER_STATUS_UPDATED in registered_types


async def test_dm_missing_conversation_is_noop(inbound):
    """Missing fields should silently drop rather than crash."""
    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {},
        )
    )
    await inbound._on_dm_deleted(
        _event(
            FederationEventType.DM_MESSAGE_DELETED,
            {},
        )
    )
    await inbound._on_dm_reaction(
        _event(
            FederationEventType.DM_MESSAGE_REACTION,
            {},
        )
    )


async def test_user_removed_without_existing_remote_is_noop(inbound):
    """USER_REMOVED for an unknown user does not raise."""
    await inbound._on_user_removed(
        _event(
            FederationEventType.USER_REMOVED,
            {"user_id": "never-seen"},
        )
    )


async def test_user_removed_cascades_to_moments_highlights_and_dms(db, bus):
    """USER_REMOVED purges every moment, highlight, and conversation
    the deprovisioned user is involved in. Matches the "hide = remove"
    semantic the SPA copy promises in ConnectionDetail."""
    from datetime import timedelta

    from socialhome.domain.conversation import (
        Conversation,
        ConversationMessage,
        ConversationType,
    )
    from socialhome.domain.highlight import Highlight, HighlightFrame
    from socialhome.domain.moment import Moment
    from socialhome.repositories.highlight_repo import SqliteHighlightRepo
    from socialhome.repositories.moment_repo import SqliteMomentRepo

    moment_repo = SqliteMomentRepo(db)
    highlight_repo = SqliteHighlightRepo(db)
    conv_repo = SqliteConversationRepo(db)

    inbound = FederationInboundService(
        bus=bus,
        conversation_repo=conv_repo,
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        highlight_repo=highlight_repo,
        moment_repo=moment_repo,
    )

    hidden_uid = "u-hidden"
    other_uid = "u-other"
    now = datetime.now(timezone.utc)

    # Seed: a moment from the hidden user and one from someone else.
    await moment_repo.save(
        Moment(
            id="m-hidden",
            author_user_id=hidden_uid,
            content="hidden-author moment",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            origin_instance_id="peer-a",
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=1)).isoformat(),
            is_public=False,
            received_via="household",
        )
    )
    await moment_repo.save(
        Moment(
            id="m-other",
            author_user_id=other_uid,
            content="other moment",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            origin_instance_id="peer-a",
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=1)).isoformat(),
            is_public=False,
            received_via="household",
        )
    )

    # Seed: a highlight from the hidden user + one frame.
    from socialhome.domain.highlight import (
        HighlightAudience,
        HighlightFrameType,
    )

    await highlight_repo.save_highlight(
        Highlight(
            id="h-hidden",
            author_user_id=hidden_uid,
            highlight_date=now.date().isoformat(),
            audience_kind=HighlightAudience.ALL_PAIRED,
            audience=(),
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=1)).isoformat(),
        )
    )
    await highlight_repo.save_frame(
        HighlightFrame(
            id="f-hidden",
            highlight_id="h-hidden",
            sequence=0,
            frame_type=HighlightFrameType.IMAGE,
            media_url="https://example.invalid/h.jpg",
            caption_text="hidden",
            caption_emoji=None,
            duration_ms=None,
            created_at=now.isoformat(),
        )
    )

    # Seed: a conversation with a message from the hidden user. Plus
    # a second conversation untouched by the hidden user to confirm
    # we don't over-purge.
    await conv_repo.create(
        Conversation(
            id="conv-touched",
            type=ConversationType.DM,
            created_at=now,
        )
    )
    await conv_repo.save_message(
        ConversationMessage(
            id="msg-1",
            conversation_id="conv-touched",
            sender_user_id=hidden_uid,
            content="hi from hidden",
            created_at=now,
        )
    )
    await conv_repo.create(
        Conversation(
            id="conv-clean",
            type=ConversationType.DM,
            created_at=now,
        )
    )
    await conv_repo.save_message(
        ConversationMessage(
            id="msg-2",
            conversation_id="conv-clean",
            sender_user_id=other_uid,
            content="hi from other",
            created_at=now,
        )
    )

    # Fire USER_REMOVED for the hidden user.
    await inbound._on_user_removed(
        _event(
            FederationEventType.USER_REMOVED,
            {"user_id": hidden_uid},
        )
    )

    # Hidden user's moment is gone; other user's moment survives.
    assert await moment_repo.get("m-hidden") is None
    assert await moment_repo.get("m-other") is not None

    # Hidden user's highlight is gone.
    assert await highlight_repo.get_highlight("h-hidden") is None

    # Conversation touched by the hidden user is hard-deleted; the
    # clean one survives.
    assert await conv_repo.get("conv-touched") is None
    assert await conv_repo.get("conv-clean") is not None


async def test_space_comment_created_persists_and_publishes(db, bus, inbound):
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "sp-1",
            "Space 1",
            "peer-a",
            "owner",
            "aa" * 32,
            SpaceType.HOUSEHOLD.value,
            JoinMode.INVITE_ONLY.value,
        ),
    )
    repo = SqliteSpacePostRepo(db)
    await repo.save(
        "sp-1",
        Post(
            id="p-1",
            author="u",
            type=PostType.TEXT,
            created_at=datetime.now(timezone.utc),
            content="post",
        ),
    )
    captured: list[CommentAdded] = []
    bus.subscribe(CommentAdded, captured.append)

    await inbound._on_space_comment_added(
        _event(
            FederationEventType.SPACE_COMMENT_CREATED,
            {
                "post_id": "p-1",
                "comment_id": "c-1",
                "author": "u-r",
                "type": "text",
                "content": "nice",
            },
        )
    )
    row = await db.fetchone(
        "SELECT content FROM space_post_comments WHERE id=?",
        ("c-1",),
    )
    assert row["content"] == "nice"
    assert len(captured) == 1


async def test_space_report_inbound_persists_remote_report(db, bus):
    """Inbound SPACE_REPORT calls through to ReportService, landing a row
    with ``reporter_instance_id = event.from_instance``.
    """
    from socialhome.repositories.report_repo import SqliteReportRepo
    from socialhome.services.report_service import ReportService

    report_repo = SqliteReportRepo(db)
    report_service = ReportService(
        report_repo=report_repo,
        user_repo=SqliteUserRepo(db),
        bus=bus,
    )
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        report_service=report_service,
    )
    await svc._on_space_report(
        _event(
            FederationEventType.SPACE_REPORT,
            {
                "target_type": "post",
                "target_id": "p-remote",
                "category": "spam",
                "notes": "looks sketchy",
                "reporter_user_id": "u-remote",
            },
            from_instance="peer-a",
        )
    )
    rows = await db.fetchall(
        "SELECT reporter_user_id, reporter_instance_id, category FROM content_reports",
    )
    assert len(rows) == 1
    assert rows[0]["reporter_user_id"] == "u-remote"
    assert rows[0]["reporter_instance_id"] == "peer-a"
    assert rows[0]["category"] == "spam"


async def test_space_report_inbound_noop_without_report_service(db, bus):
    """If ReportService isn't attached, the handler logs + returns cleanly."""
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        report_service=None,
    )
    await svc._on_space_report(
        _event(
            FederationEventType.SPACE_REPORT,
            {
                "target_type": "post",
                "target_id": "p",
                "category": "spam",
                "reporter_user_id": "u",
            },
        )
    )
    rows = await db.fetchall("SELECT 1 FROM content_reports")
    assert rows == []


async def test_users_sync_upserts_remote_users(db, inbound):
    # remote_users has FK to remote_instances — seed the peer row first.
    await db.enqueue(
        """INSERT INTO remote_instances(
               id, display_name, remote_identity_pk,
               key_self_to_remote, key_remote_to_self,
               remote_inbox_url, local_inbox_id,
               status, source, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            "peer-a",
            "Peer A",
            "aa" * 32,
            "enc",
            "enc",
            "https://peer/wh",
            "wh-peer-a",
            "confirmed",
            "manual",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    await inbound._on_users_sync(
        _event(
            FederationEventType.USERS_SYNC,
            {
                "users": [
                    {"user_id": "u-r1", "username": "alice", "display_name": "Alice"},
                    {"user_id": "u-r2", "username": "bob", "display_name": "Bob"},
                ]
            },
            from_instance="peer-a",
        )
    )
    rows = await db.fetchall(
        "SELECT user_id, instance_id, remote_username FROM remote_users ORDER BY user_id",
    )
    assert [r["user_id"] for r in rows] == ["u-r1", "u-r2"]
    assert rows[0]["instance_id"] == "peer-a"
    assert rows[0]["remote_username"] == "alice"


# ─── SPACE_MEDIA_BLOB — write the bytes the sender shipped ─────────


async def test_space_media_blob_writes_bytes_to_local_media_dir(
    db,
    bus,
    tmp_path,
):
    """The inbound receiver writes the bytes under the SAME filename
    the sender used so the relative ``<img src>`` on the post row
    resolves on this household. Without this handler the post lands
    but the picture is broken."""
    import base64

    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        media_dir=tmp_path,
    )
    body = b"WEBP-bytes-from-the-sender"
    await service._on_space_media_blob(
        _event(
            FederationEventType.SPACE_MEDIA_BLOB,
            {
                "post_id": "post-mediated",
                "space_id": "sp-mediated",
                "filename": "abc123.webp",
                "mime_type": "image/webp",
                "bytes_base64": base64.b64encode(body).decode("ascii"),
            },
        )
    )
    assert (tmp_path / "abc123.webp").read_bytes() == body


async def test_space_media_blob_prefers_raw_media_bytes(db, bus, tmp_path):
    """Binary ``fed-media-v1`` path: ``event.media_bytes`` is used and
    written verbatim, with NO base64 field in the payload."""
    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        media_dir=tmp_path,
    )
    body = b"\x00\x01raw-binary-no-base64\xff"
    await service._on_space_media_blob(
        _event(
            FederationEventType.SPACE_MEDIA_BLOB,
            {
                "post_id": "post-bin",
                "space_id": "sp-bin",
                "filename": "binary.webp",
                "transfer_id": "tx-1",
                "chunk_count": 1,
                "final": True,
            },
            media_bytes=body,
        )
    )
    assert (tmp_path / "binary.webp").read_bytes() == body


async def test_dm_media_blob_prefers_raw_media_bytes(db, bus, tmp_path):
    """DM binary path: ``event.media_bytes`` lands as the final file and
    flips ``media_sync_status`` even with no ``bytes_b64`` present."""
    convo_repo = SqliteConversationRepo(db)
    service = FederationInboundService(
        bus=bus,
        conversation_repo=convo_repo,
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        media_dir=tmp_path,
    )
    body = b"\x89PNG raw dm media bytes"
    await service._on_dm_media_blob(
        _event(
            FederationEventType.DM_MEDIA_BLOB,
            {
                "media_blob_id": "mblob-1",
                "message_id": "mblob-1",
                "conversation_id": "c-1",
                "mime_type": "image/png",
                "chunk_index": 0,
                "chunk_count": 1,
                "final": True,
            },
            media_bytes=body,
        )
    )
    # The single-chunk fast path writes ``<message_id><ext>``.
    written = tmp_path / "mblob-1.png"
    assert written.read_bytes() == body


async def test_space_media_blob_rejects_path_traversal(db, bus, tmp_path):
    """A malicious sender could ship ``filename: '../../etc/passwd'``;
    same allowlist as ``MediaServeView`` rejects ``/``, ``\\``, or
    leading ``.``."""
    import base64

    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        media_dir=tmp_path,
    )
    body = b"injection-attempt"
    for bad in ("../escape.webp", "sub/dir.webp", ".hidden.webp"):
        await service._on_space_media_blob(
            _event(
                FederationEventType.SPACE_MEDIA_BLOB,
                {
                    "post_id": "p",
                    "filename": bad,
                    "bytes_base64": base64.b64encode(body).decode("ascii"),
                },
            )
        )
    # Nothing was written outside the media dir.
    contents = list(tmp_path.iterdir())
    assert contents == [] or all(
        c.name not in ("escape.webp", "dir.webp", "hidden.webp") for c in contents
    )


async def test_space_media_blob_no_media_dir_drops(db, bus):
    """Receiver without ``media_dir`` (test stack) silently no-ops
    rather than crashing."""
    import base64

    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    # Should not raise.
    await service._on_space_media_blob(
        _event(
            FederationEventType.SPACE_MEDIA_BLOB,
            {
                "filename": "x.webp",
                "bytes_base64": base64.b64encode(b"x").decode("ascii"),
            },
        )
    )


async def test_space_media_blob_assembles_chunked_transfer(db, bus, tmp_path):
    """Multi-chunk transfer: each chunk lands in a per-transfer
    partial dir; the final chunk triggers concat into the target
    filename. Mirrors :class:`SpaceMediaSyncService`'s sender shape."""
    import base64

    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        media_dir=tmp_path,
    )
    full = b"".join(bytes([i]) * 256 for i in range(8))  # 2 KiB
    # Split into 4 chunks of 512 bytes each.
    chunks = [full[i * 512 : (i + 1) * 512] for i in range(4)]
    for idx, chunk in enumerate(chunks):
        await service._on_space_media_blob(
            _event(
                FederationEventType.SPACE_MEDIA_BLOB,
                {
                    "post_id": "p",
                    "transfer_id": "tx-1",
                    "filename": "big.webm",
                    "chunk_index": idx,
                    "chunk_count": 4,
                    "final": idx == 3,
                    "bytes_b64": base64.b64encode(chunk).decode("ascii"),
                },
            )
        )
    assert (tmp_path / "big.webm").read_bytes() == full
    # Partial directory cleaned up.
    assert not (tmp_path / ".partial" / "tx-1").exists()


# ─── Space config sequence gate (PR follow-up to #459) ────────────────


async def _seed_space(db, *, space_id="sp-cfg", from_instance="peer-a", seq=0):
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode,
                              config_sequence)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            space_id,
            "Cfg Space",
            from_instance,
            "owner",
            "aa" * 32,
            SpaceType.PRIVATE.value,
            JoinMode.INVITE_ONLY.value,
            seq,
        ),
    )


def _cfg_event(*, space_id, from_instance, sequence, name):
    """SPACE_CONFIG_CHANGED envelope shaped like SpaceConfigOutbound (#459).

    Carries both the flat legacy fields AND ``space_meta`` (modern shape)
    plus a top-level ``sequence`` — matches the on-wire shape the
    receiver guard reads.
    """
    return _event(
        FederationEventType.SPACE_CONFIG_CHANGED,
        {
            "space_id": space_id,
            "sequence": sequence,
            "event_type": "rename",
            "name": name,
            "join_mode": "invite_only",
            "space_type": "private",
            "features": {
                "calendar": True,
                "todo": True,
                "location": False,
                "location_mode": "gps",
                "stickies": True,
                "pages": True,
                "gallery": True,
            },
            "space_meta": {
                "name": name,
                "owner_instance_id": from_instance,
                "owner_username": "owner",
                "identity_public_key": "aa" * 32,
                "config_sequence": sequence,
                "space_type": "private",
                "join_mode": "invite_only",
                "features": {
                    "calendar": True,
                    "todo": True,
                    "location": False,
                    "location_mode": "gps",
                    "stickies": True,
                    "pages": True,
                    "gallery": True,
                },
            },
        },
        from_instance=from_instance,
        space_id=space_id,
    )


async def test_space_config_changed_applies_when_sequence_advances(
    db,
    bus,
    inbound,
):
    await _seed_space(db, space_id="sp-cfg", from_instance="peer-a", seq=5)
    await inbound._on_space_config_changed(
        _cfg_event(
            space_id="sp-cfg",
            from_instance="peer-a",
            sequence=6,
            name="Renamed",
        ),
    )
    row = await db.fetchone(
        "SELECT name, config_sequence FROM spaces WHERE id=?",
        ("sp-cfg",),
    )
    assert row["name"] == "Renamed"
    assert row["config_sequence"] == 6


async def test_space_config_changed_dropped_on_stale_sequence(
    db,
    bus,
    inbound,
):
    """Out-of-order delivery: an older snapshot must not clobber a newer
    one. The receiver guards on ``incoming.sequence > existing``."""
    await _seed_space(db, space_id="sp-cfg", from_instance="peer-a", seq=10)
    await inbound._on_space_config_changed(
        _cfg_event(
            space_id="sp-cfg",
            from_instance="peer-a",
            sequence=7,  # stale
            name="StaleName",
        ),
    )
    row = await db.fetchone(
        "SELECT name, config_sequence FROM spaces WHERE id=?",
        ("sp-cfg",),
    )
    # No clobber.
    assert row["name"] == "Cfg Space"
    assert row["config_sequence"] == 10


async def test_space_config_changed_dropped_on_equal_sequence(
    db,
    bus,
    inbound,
):
    """Equal sequence = same state, no-op. Don't waste an UPSERT."""
    await _seed_space(db, space_id="sp-cfg", from_instance="peer-a", seq=4)
    await inbound._on_space_config_changed(
        _cfg_event(
            space_id="sp-cfg",
            from_instance="peer-a",
            sequence=4,
            name="ShouldNotApply",
        ),
    )
    row = await db.fetchone(
        "SELECT name FROM spaces WHERE id=?",
        ("sp-cfg",),
    )
    assert row["name"] == "Cfg Space"


async def test_space_config_changed_missing_sequence_falls_through(
    db,
    bus,
    inbound,
):
    """Older sender doesn't ship ``sequence`` — the guard treats this
    as "no sequence available" and applies the upsert anyway. Otherwise
    a sender's omission would silently lock the receiver out of legit
    updates.
    """
    await _seed_space(db, space_id="sp-cfg", from_instance="peer-a", seq=2)
    event = _cfg_event(
        space_id="sp-cfg",
        from_instance="peer-a",
        sequence=99,  # value here ignored — we strip the field below
        name="ShouldApply",
    )
    del event.payload["sequence"]
    del event.payload["space_meta"]["config_sequence"]
    await inbound._on_space_config_changed(event)
    row = await db.fetchone(
        "SELECT name FROM spaces WHERE id=?",
        ("sp-cfg",),
    )
    assert row["name"] == "ShouldApply"


async def test_space_config_changed_wrong_owner_drops(db, bus, inbound):
    """Defence-in-depth: only the row's owner_instance_id can update
    via this path. A second peer can't spoof a config change for
    somebody else's space."""
    await _seed_space(db, space_id="sp-cfg", from_instance="peer-a", seq=5)
    await inbound._on_space_config_changed(
        _cfg_event(
            space_id="sp-cfg",
            from_instance="peer-b",  # not the owner
            sequence=10,
            name="Spoofed",
        ),
    )
    row = await db.fetchone(
        "SELECT name FROM spaces WHERE id=?",
        ("sp-cfg",),
    )
    assert row["name"] == "Cfg Space"


async def test_space_config_changed_ignored_after_remote_dissolve(db, bus, inbound):
    """Terminal state: once the host dissolved this space (locally archived
    with ``archived_reason='dissolved'``), a LATER higher-sequence
    SPACE_CONFIG_CHANGED with ``archived=False`` must NOT revive it. The
    snapshot is ignored — the space stays archived with its dissolve reason
    intact."""
    await _seed_space(db, space_id="sp-cfg", from_instance="peer-a", seq=5)
    # Host dissolved → member archived its local copy read-only.
    await db.enqueue(
        "UPDATE spaces SET archived=1, archived_reason='dissolved' WHERE id=?",
        ("sp-cfg",),
    )
    event = _cfg_event(
        space_id="sp-cfg",
        from_instance="peer-a",
        sequence=9,  # higher than existing 5
        name="Revived",
    )
    # A revival attempt would carry archived=False in the snapshot.
    event.payload["space_meta"]["archived"] = False
    await inbound._on_space_config_changed(event)
    row = await db.fetchone(
        "SELECT name, archived, archived_reason FROM spaces WHERE id=?",
        ("sp-cfg",),
    )
    assert row["name"] == "Cfg Space"  # snapshot ignored, no rename
    assert row["archived"] == 1
    assert row["archived_reason"] == "dissolved"


# ─── v_24: authority-signed config from a non-owner delegated admin ──────


async def _seed_signed_space(db, *, space_id, owner_instance, space_pub_hex, seq=0):
    """Seed a stub whose ``identity_public_key`` is a REAL Ed25519 pub so an
    authority signature actually verifies."""
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode,
                              config_sequence)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            space_id,
            "Cfg Space",
            owner_instance,
            "owner",
            space_pub_hex,
            SpaceType.PRIVATE.value,
            JoinMode.INVITE_ONLY.value,
            seq,
        ),
    )


def _signed_cfg_event(
    *,
    space_id,
    from_instance,
    owner_instance,
    sequence,
    name,
    seed,
    author=None,
    tamper=False,
    bad_sig=False,
):
    """Build a SPACE_CONFIG_CHANGED whose ``space_meta`` is authority-signed
    with the space seed. ``author`` records the config_author_instance carried
    in the (signed) meta; defaults to ``from_instance``."""
    from socialhome.services.space_crypto_service import (
        sign_authority_event,
        strip_authority_sig_fields,
    )

    author = author if author is not None else from_instance
    meta = {
        "name": name,
        "owner_instance_id": owner_instance,
        "owner_username": "owner",
        "identity_public_key": "ignored-by-stub",
        "config_sequence": sequence,
        "config_author_instance": author,
        "space_type": "private",
        "join_mode": "invite_only",
        "features": {
            "calendar": True,
            "todo": True,
            "location": False,
            "location_mode": "gps",
            "stickies": True,
            "pages": True,
            "gallery": True,
        },
    }
    signed = sign_authority_event(
        event_type="space_config_changed",
        space_id=space_id,
        payload=strip_authority_sig_fields(meta),
        space_seed=seed,
    )
    meta.update(signed)
    if tamper:
        # Mutate a signed field AFTER signing → signature no longer matches.
        meta["name"] = name + " (tampered)"
    if bad_sig:
        meta["authority_sig"] = "AAAA" + meta["authority_sig"][4:]
    return _event(
        FederationEventType.SPACE_CONFIG_CHANGED,
        {
            "space_id": space_id,
            "sequence": sequence,
            "event_type": "rename",
            "space_meta": meta,
        },
        from_instance=from_instance,
        space_id=space_id,
    )


async def test_config_from_nonowner_authority_signed_applies(db, bus, inbound):
    """A delegated admin (NOT the owner) signs a config change with the space
    seed; the receiver accepts it by verifying against the space pubkey, NOT by
    checking from_instance == owner."""
    from socialhome.crypto import generate_space_keypair

    kp = generate_space_keypair()
    await _seed_signed_space(
        db,
        space_id="sp-auth",
        owner_instance="owner-i",
        space_pub_hex=kp.public_key.hex(),
        seq=5,
    )
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-auth",
            from_instance="admin-i",  # a delegated admin household, not owner
            owner_instance="owner-i",
            sequence=6,
            name="RenamedByAdmin",
            seed=kp.private_key,
        )
    )
    row = await db.fetchone(
        "SELECT name, config_sequence, config_author_instance FROM spaces WHERE id=?",
        ("sp-auth",),
    )
    assert row["name"] == "RenamedByAdmin"
    assert row["config_sequence"] == 6
    assert row["config_author_instance"] == "admin-i"


@pytest.mark.security
async def test_config_from_nonowner_tampered_sig_drops(db, bus, inbound):
    """SECURITY: a non-owner config whose signed payload was mutated after
    signing must be DROPPED — the signature no longer verifies."""
    from socialhome.crypto import generate_space_keypair

    kp = generate_space_keypair()
    await _seed_signed_space(
        db,
        space_id="sp-auth",
        owner_instance="owner-i",
        space_pub_hex=kp.public_key.hex(),
        seq=5,
    )
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-auth",
            from_instance="admin-i",
            owner_instance="owner-i",
            sequence=6,
            name="Evil",
            seed=kp.private_key,
            tamper=True,
        )
    )
    row = await db.fetchone(
        "SELECT name, config_sequence FROM spaces WHERE id=?",
        ("sp-auth",),
    )
    assert row["name"] == "Cfg Space"  # dropped, no apply
    assert row["config_sequence"] == 5


@pytest.mark.security
async def test_config_from_nonowner_bad_sig_drops(db, bus, inbound):
    """SECURITY: a non-owner config with a corrupt authority_sig is DROPPED —
    a present-but-invalid sig never falls through to the owner gate."""
    from socialhome.crypto import generate_space_keypair

    kp = generate_space_keypair()
    await _seed_signed_space(
        db,
        space_id="sp-auth",
        owner_instance="owner-i",
        space_pub_hex=kp.public_key.hex(),
        seq=5,
    )
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-auth",
            from_instance="admin-i",
            owner_instance="owner-i",
            sequence=6,
            name="Evil",
            seed=kp.private_key,
            bad_sig=True,
        )
    )
    row = await db.fetchone(
        "SELECT name FROM spaces WHERE id=?",
        ("sp-auth",),
    )
    assert row["name"] == "Cfg Space"


async def test_config_from_nonowner_no_sig_drops(db, bus, inbound):
    """A non-owner config with NO authority signature is dropped (today's
    owner-only behaviour) — only a signature relaxes the gate."""
    await _seed_space(db, space_id="sp-cfg", from_instance="peer-a", seq=5)
    await inbound._on_space_config_changed(
        _cfg_event(
            space_id="sp-cfg",
            from_instance="peer-b",  # not owner, no sig
            sequence=10,
            name="Spoofed",
        )
    )
    row = await db.fetchone("SELECT name FROM spaces WHERE id=?", ("sp-cfg",))
    assert row["name"] == "Cfg Space"


async def test_config_from_owner_without_sig_still_applies(db, bus, inbound):
    """Back-compat: an owner-originated config with no authority signature
    still applies through the legacy from_instance == owner path."""
    await _seed_space(db, space_id="sp-cfg", from_instance="peer-a", seq=5)
    await inbound._on_space_config_changed(
        _cfg_event(
            space_id="sp-cfg",
            from_instance="peer-a",  # the owner
            sequence=6,
            name="OwnerRename",
        )
    )
    row = await db.fetchone("SELECT name FROM spaces WHERE id=?", ("sp-cfg",))
    assert row["name"] == "OwnerRename"


async def test_config_lww_same_sequence_deterministic_tiebreak(db, bus, inbound):
    """Two concurrent same-sequence edits from different admins converge to the
    SAME winner regardless of arrival order — the higher author id wins; a
    lower-(seq,author) is dropped. Run both orderings and assert identical
    final state."""
    from socialhome.crypto import generate_space_keypair

    async def _converge(first_author, second_author):
        kp = generate_space_keypair()
        sid = f"sp-lww-{first_author}-{second_author}"
        await _seed_signed_space(
            db,
            space_id=sid,
            owner_instance="owner-i",
            space_pub_hex=kp.public_key.hex(),
            seq=5,
        )
        # Both edits bump from base 5 → seq 6, different authors.
        await inbound._on_space_config_changed(
            _signed_cfg_event(
                space_id=sid,
                from_instance=first_author,
                owner_instance="owner-i",
                sequence=6,
                name=f"By-{first_author}",
                author=first_author,
                seed=kp.private_key,
            )
        )
        await inbound._on_space_config_changed(
            _signed_cfg_event(
                space_id=sid,
                from_instance=second_author,
                owner_instance="owner-i",
                sequence=6,
                name=f"By-{second_author}",
                author=second_author,
                seed=kp.private_key,
            )
        )
        row = await db.fetchone(
            "SELECT name, config_author_instance FROM spaces WHERE id=?",
            (sid,),
        )
        return row["name"], row["config_author_instance"]

    # Higher author id ("admin-z") must win in both arrival orders.
    order1 = await _converge("admin-a", "admin-z")
    order2 = await _converge("admin-z", "admin-a")
    assert order1 == order2 == ("By-admin-z", "admin-z")


async def test_config_lww_lower_author_at_equal_seq_dropped(db, bus, inbound):
    """A second edit at the same sequence but a LOWER author id is dropped."""
    from socialhome.crypto import generate_space_keypair

    kp = generate_space_keypair()
    await _seed_signed_space(
        db,
        space_id="sp-lww2",
        owner_instance="owner-i",
        space_pub_hex=kp.public_key.hex(),
        seq=5,
    )
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-lww2",
            from_instance="admin-z",
            owner_instance="owner-i",
            sequence=6,
            name="ByZ",
            author="admin-z",
            seed=kp.private_key,
        )
    )
    # Lower author, same seq → must NOT clobber.
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-lww2",
            from_instance="admin-a",
            owner_instance="owner-i",
            sequence=6,
            name="ByA",
            author="admin-a",
            seed=kp.private_key,
        )
    )
    row = await db.fetchone("SELECT name FROM spaces WHERE id=?", ("sp-lww2",))
    assert row["name"] == "ByZ"
