"""Tests for socialhome.services.notification_service."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.crypto import generate_identity_keypair, derive_instance_id
from socialhome.db.database import AsyncDatabase
from socialhome.domain.post import PostType
from socialhome.domain.task import Task, TaskStatus
from socialhome.domain.events import TaskAssigned
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.notification_repo import SqliteNotificationRepo
from socialhome.repositories.post_repo import SqlitePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.feed_service import FeedService
from socialhome.services.notification_service import NotificationService
from socialhome.services.user_service import UserService


@pytest.fixture
async def stack(tmp_dir):
    """Full service stack for notification service tests."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        """INSERT INTO instance_identity(instance_id, identity_private_key,
           identity_public_key, routing_secret) VALUES(?,?,?,?)""",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    post_repo = SqlitePostRepo(db)
    space_repo = SqliteSpaceRepo(db)
    notif_repo = SqliteNotificationRepo(db, max_per_user=50)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    feed_svc = FeedService(post_repo, user_repo, bus)
    notif_svc = NotificationService(notif_repo, user_repo, space_repo, bus)
    notif_svc.wire()

    class Stack:
        pass

    s = Stack()
    s.db = db
    s.user_svc = user_svc
    s.feed_svc = feed_svc
    s.notif_svc = notif_svc
    s.notif_repo = notif_repo
    s.bus = bus

    async def provision_user(username, **kw):
        return await user_svc.provision(username=username, display_name=username, **kw)

    s.provision_user = provision_user
    yield s
    await db.shutdown()


async def test_post_created_notifies_others(stack):
    """Creating a feed post sends a notification to other users, not the author."""
    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    await stack.feed_svc.create_post(
        author_user_id=a.user_id,
        type=PostType.TEXT,
        content="hi",
    )
    bob_n = await stack.notif_repo.list(b.user_id, limit=10)
    anna_n = await stack.notif_repo.list(a.user_id, limit=10)
    assert len(bob_n) >= 1
    assert len(anna_n) == 0


async def test_task_assigned(stack):
    """TaskAssigned event generates a notification for the assignee."""
    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    now = datetime.now(timezone.utc)
    evt = TaskAssigned(
        task=Task(
            id="t1",
            list_id="l1",
            title="Buy milk",
            status=TaskStatus.TODO,
            position=0,
            created_by=a.user_id,
            created_at=now,
            updated_at=now,
        ),
        assigned_to=b.user_id,
    )
    await stack.bus.publish(evt)
    bob_n = await stack.notif_repo.list(b.user_id, limit=10)
    assert any("Buy milk" in n.title for n in bob_n)


async def test_self_assign_no_notification(stack):
    """Assigning a task to yourself does not generate a notification."""
    a = await stack.provision_user("anna")
    now = datetime.now(timezone.utc)
    evt = TaskAssigned(
        task=Task(
            id="t1",
            list_id="l1",
            title="Self",
            status=TaskStatus.TODO,
            position=0,
            created_by=a.user_id,
            created_at=now,
            updated_at=now,
        ),
        assigned_to=a.user_id,
    )
    pre = len(await stack.notif_repo.list(a.user_id, limit=50))
    await stack.bus.publish(evt)
    post = len(await stack.notif_repo.list(a.user_id, limit=50))
    assert post == pre


async def test_comment_notifies_others(stack):
    """CommentAdded notifies all household members except the commenter."""
    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    c = await stack.provision_user("carl")
    post = await stack.feed_svc.create_post(
        author_user_id=a.user_id, type=PostType.TEXT, content="hi"
    )
    # Clear notifications from post creation
    for u in [a, b, c]:
        await stack.notif_repo.mark_all_read(u.user_id)
    # Bob comments
    await stack.feed_svc.add_comment(post.id, author_user_id=b.user_id, content="nice")
    # Anna and Carl should get a comment notification, not Bob
    anna_n = await stack.notif_repo.list(a.user_id, limit=50)
    carl_n = await stack.notif_repo.list(c.user_id, limit=50)
    bob_n = await stack.notif_repo.list(b.user_id, limit=50)
    assert any("commented" in n.title for n in anna_n)
    assert any("commented" in n.title for n in carl_n)
    assert not any("commented" in n.title and n.read_at is None for n in bob_n)


async def test_space_post_notifies_members(stack):
    """SpacePostCreated notifies space members except the author."""
    from socialhome.repositories.space_repo import SqliteSpaceRepo
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
    from socialhome.services.space_service import SpaceService

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space_repo = SqliteSpaceRepo(stack.db)
    spost_repo = SqliteSpacePostRepo(stack.db)
    space_svc = SpaceService(
        space_repo,
        spost_repo,
        SqliteUserRepo(stack.db),
        stack.bus,
        own_instance_id="iid",
    )
    space = await space_svc.create_space(owner_username="anna", name="S")
    await space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await space_svc.create_post(
        space.id, author_user_id=a.user_id, type=PostType.TEXT, content="space hello"
    )
    bob_n = await stack.notif_repo.list(b.user_id, limit=50)
    assert any("posted in S" in n.title for n in bob_n)


async def test_space_post_respects_muted_notif_pref(stack):
    """Members with level='muted' receive no space-post notification."""
    from socialhome.repositories.space_repo import SqliteSpaceRepo
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
    from socialhome.services.space_service import SpaceService

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space_repo = SqliteSpaceRepo(stack.db)
    spost_repo = SqliteSpacePostRepo(stack.db)
    space_svc = SpaceService(
        space_repo,
        spost_repo,
        SqliteUserRepo(stack.db),
        stack.bus,
        own_instance_id="iid",
    )
    space = await space_svc.create_space(owner_username="anna", name="Quiet")
    await space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await stack.notif_repo.set_space_notif_level(
        user_id=b.user_id,
        space_id=space.id,
        level="muted",
    )
    await space_svc.create_post(
        space.id, author_user_id=a.user_id, type=PostType.TEXT, content="shhh"
    )
    bob_n = await stack.notif_repo.list(b.user_id, limit=50)
    assert not any("posted in Quiet" in n.title for n in bob_n)


async def test_space_post_mentions_only_skips_non_mention(stack):
    """level='mentions' — drops non-mention posts, keeps mentions."""
    from socialhome.domain.events import SpacePostCreated
    from socialhome.domain.mention import Mention, MentionType
    from socialhome.domain.post import Post
    from socialhome.repositories.space_repo import SqliteSpaceRepo
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
    from socialhome.services.space_service import SpaceService

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space_repo = SqliteSpaceRepo(stack.db)
    spost_repo = SqliteSpacePostRepo(stack.db)
    space_svc = SpaceService(
        space_repo,
        spost_repo,
        SqliteUserRepo(stack.db),
        stack.bus,
        own_instance_id="iid",
    )
    space = await space_svc.create_space(owner_username="anna", name="M")
    await space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await stack.notif_repo.set_space_notif_level(
        user_id=b.user_id,
        space_id=space.id,
        level="mentions",
    )
    # Plain post — should NOT notify bob.
    await space_svc.create_post(
        space.id, author_user_id=a.user_id, type=PostType.TEXT, content="hi all"
    )
    bob_n = await stack.notif_repo.list(b.user_id, limit=50)
    assert not any(n.type == "space_post_created" for n in bob_n)
    # Post with bob mention — publish event directly with mentions=...
    post = Post(
        id="p-mention",
        author=a.user_id,
        type=PostType.TEXT,
        content="@bob!",
        created_at=datetime.now(timezone.utc),
    )
    await stack.bus.publish(
        SpacePostCreated(
            post=post,
            space_id=space.id,
            mentions=(
                Mention(
                    type=MentionType.USER,
                    raw="@bob",
                    user_id=b.user_id,
                ),
            ),
        )
    )
    bob_n = await stack.notif_repo.list(b.user_id, limit=50)
    assert any(n.type == "space_post_created" for n in bob_n)


async def test_moderation_queued_notifies_admins(stack):
    """SpaceModerationQueued notifies space admins."""
    from socialhome.repositories.space_repo import SqliteSpaceRepo
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
    from socialhome.services.space_service import SpaceService
    from socialhome.domain.space import SpaceFeatures, SpaceFeatureAccess

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space_repo = SqliteSpaceRepo(stack.db)
    spost_repo = SqliteSpacePostRepo(stack.db)
    space_svc = SpaceService(
        space_repo,
        spost_repo,
        SqliteUserRepo(stack.db),
        stack.bus,
        own_instance_id="iid",
    )
    space = await space_svc.create_space(owner_username="anna", name="Mod")
    await space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED),
    )
    # Bob is regular member — post goes to queue → admin (anna) gets notification
    result = await space_svc.create_post(
        space.id, author_user_id=b.user_id, type=PostType.TEXT, content="pending"
    )
    assert result is None  # queued
    anna_n = await stack.notif_repo.list(a.user_id, limit=50)
    assert any("pending review" in n.title for n in anna_n)


async def test_task_deadline_notifies_assignees(stack):
    """TaskDeadlineDue notifies all assignees."""
    from datetime import date
    from socialhome.domain.events import TaskDeadlineDue

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    now = datetime.now(timezone.utc)
    evt = TaskDeadlineDue(
        task=Task(
            id="t1",
            list_id="l1",
            title="Deadline task",
            status=TaskStatus.TODO,
            position=0,
            created_by="other",
            created_at=now,
            updated_at=now,
            assignees=(a.user_id, b.user_id),
        ),
        due_date=date.today(),
    )
    await stack.bus.publish(evt)
    anna_n = await stack.notif_repo.list(a.user_id, limit=50)
    bob_n = await stack.notif_repo.list(b.user_id, limit=50)
    assert any("due today" in n.title for n in anna_n)
    assert any("due today" in n.title for n in bob_n)


# ─── Push fan-out (§25.3) ─────────────────────────────────────────────────


class _CapturingPush:
    """Fake PushService for assert-pushed tests.

    Captures both fan-out shapes:
    * ``push_to_users(ids, payload)`` — used by ``_fan_push``.
    * ``push_to_user(id, payload)``  — used by ``_save_notif`` for
      the per-row Web Push fan-out.

    The combined ``calls`` log preserves the order so a single test
    can assert across both code paths.
    """

    def __init__(self):
        self.calls: list[tuple[list[str], object]] = []

    async def push_to_users(self, user_ids, payload):
        self.calls.append((list(user_ids), payload))
        return len(user_ids)

    async def push_to_user(self, user_id, payload):
        self.calls.append(([user_id], payload))
        return 1


async def test_dm_message_creates_in_app_row_and_push(stack):
    """A new DM creates an in-app notification row per recipient (so
    the bell renders an unread badge) AND fires push (§25.3 — title
    only, no body)."""
    from socialhome.domain.events import DmMessageCreated

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    fake = _CapturingPush()
    stack.notif_svc.attach_push_service(fake)

    await stack.bus.publish(
        DmMessageCreated(
            conversation_id="c-1",
            message_id="m-1",
            sender_user_id=a.user_id,
            sender_display_name="Anna",
            recipient_user_ids=(b.user_id,),
        )
    )

    # In-app row landed for the recipient.
    rows = await stack.notif_repo.list(b.user_id)
    assert len(rows) == 1
    assert rows[0].type == "dm_message"
    assert rows[0].link_url == "/dms/c-1"
    assert "Anna" in rows[0].title

    # Push went out too — title only, no body.
    assert fake.calls, "push fan-out was not triggered"
    _, payload = fake.calls[0]
    assert "Anna" in payload.title
    # §25.3: the PushPayload struct has no body field at all.
    assert not hasattr(payload, "body")


async def test_dm_message_creates_one_row_per_recipient(stack):
    """Group DMs fan one notification row to each recipient (and push
    too) so every member gets their own bell badge — bell counts
    don't get coalesced server-side."""
    from socialhome.domain.events import DmMessageCreated

    sender = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    carol = await stack.provision_user("carol")
    stack.notif_svc.attach_push_service(_CapturingPush())

    await stack.bus.publish(
        DmMessageCreated(
            conversation_id="c-group",
            message_id="m-1",
            sender_user_id=sender.user_id,
            sender_display_name="Anna",
            recipient_user_ids=(bob.user_id, carol.user_id),
        )
    )
    bob_rows = await stack.notif_repo.list(bob.user_id)
    carol_rows = await stack.notif_repo.list(carol.user_id)
    assert len(bob_rows) == 1
    assert len(carol_rows) == 1
    assert bob_rows[0].link_url == "/dms/c-group"


async def test_mark_read_for_dm_clears_unread_rows(stack):
    """``mark_read_for_dm`` flips every ``dm_message`` row pointing
    at a conversation to read — opening the thread clears the bell
    in step with the read-receipt update."""
    from socialhome.domain.events import DmMessageCreated

    sender = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    stack.notif_svc.attach_push_service(_CapturingPush())

    # Two messages from Anna to Bob → two rows.
    for mid in ("m-1", "m-2"):
        await stack.bus.publish(
            DmMessageCreated(
                conversation_id="c-1",
                message_id=mid,
                sender_user_id=sender.user_id,
                sender_display_name="Anna",
                recipient_user_ids=(bob.user_id,),
            )
        )
    assert await stack.notif_repo.count_unread(bob.user_id) == 2

    # Open the thread → both rows clear.
    n = await stack.notif_svc.mark_read_for_dm(bob.user_id, "c-1")
    assert n == 2
    assert await stack.notif_repo.count_unread(bob.user_id) == 0


async def test_mark_read_for_dm_only_touches_matching_conversation(stack):
    """A different conversation's notifications stay unread when
    one specific thread is opened."""
    from socialhome.domain.events import DmMessageCreated

    sender = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    stack.notif_svc.attach_push_service(_CapturingPush())

    for cid in ("c-1", "c-2"):
        await stack.bus.publish(
            DmMessageCreated(
                conversation_id=cid,
                message_id=f"m-{cid}",
                sender_user_id=sender.user_id,
                sender_display_name="Anna",
                recipient_user_ids=(bob.user_id,),
            )
        )
    assert await stack.notif_repo.count_unread(bob.user_id) == 2

    cleared = await stack.notif_svc.mark_read_for_dm(bob.user_id, "c-1")
    assert cleared == 1
    # The c-2 row stays unread.
    assert await stack.notif_repo.count_unread(bob.user_id) == 1


async def test_dm_message_with_no_recipients_skips_push(stack):
    from socialhome.domain.events import DmMessageCreated

    a = await stack.provision_user("anna")
    fake = _CapturingPush()
    stack.notif_svc.attach_push_service(fake)

    await stack.bus.publish(
        DmMessageCreated(
            conversation_id="c-1",
            message_id="m-1",
            sender_user_id=a.user_id,
            sender_display_name="Anna",
            recipient_user_ids=(),
        )
    )
    assert fake.calls == []


async def test_task_deadline_triggers_push(stack):
    from datetime import date
    from socialhome.domain.events import TaskDeadlineDue

    a = await stack.provision_user("anna")
    fake = _CapturingPush()
    stack.notif_svc.attach_push_service(fake)
    now = datetime.now(timezone.utc)
    evt = TaskDeadlineDue(
        task=Task(
            id="t1",
            list_id="l1",
            title="Pay bills",
            status=TaskStatus.TODO,
            position=0,
            created_by="other",
            created_at=now,
            updated_at=now,
            assignees=(a.user_id,),
        ),
        due_date=date.today(),
    )
    await stack.bus.publish(evt)
    assert fake.calls
    _, payload = fake.calls[-1]
    assert "Pay bills" in payload.title


# ─── Bazaar + DM contact handlers ─────────────────────────────────────────


async def test_bazaar_bid_placed_notifies_seller(stack):
    from socialhome.domain.events import BazaarBidPlaced

    seller = await stack.provision_user("seller")
    bidder = await stack.provision_user("bidder")
    fake = _CapturingPush()
    stack.notif_svc.attach_push_service(fake)
    await stack.bus.publish(
        BazaarBidPlaced(
            listing_post_id="L-1",
            seller_user_id=seller.user_id,
            bidder_user_id=bidder.user_id,
            amount=200,
            new_end_time="2099-01-01T00:00:00+00:00",
        )
    )
    notifs = await stack.notif_repo.list(seller.user_id, limit=10)
    assert any(n.type == "bazaar_bid_placed" for n in notifs)
    assert fake.calls
    assert fake.calls[-1][0] == [seller.user_id]


async def test_bazaar_self_bid_does_not_notify(stack):
    from socialhome.domain.events import BazaarBidPlaced

    seller = await stack.provision_user("seller")
    fake = _CapturingPush()
    stack.notif_svc.attach_push_service(fake)
    await stack.bus.publish(
        BazaarBidPlaced(
            listing_post_id="L-1",
            seller_user_id=seller.user_id,
            bidder_user_id=seller.user_id,
            amount=200,
            new_end_time="2099-01-01T00:00:00+00:00",
        )
    )
    notifs = await stack.notif_repo.list(seller.user_id, limit=10)
    assert all(n.type != "bazaar_bid_placed" for n in notifs)
    assert fake.calls == []


async def test_bazaar_offer_accepted_notifies_buyer(stack):
    from socialhome.domain.events import BazaarOfferAccepted

    seller = await stack.provision_user("seller")
    buyer = await stack.provision_user("buyer")
    fake = _CapturingPush()
    stack.notif_svc.attach_push_service(fake)
    await stack.bus.publish(
        BazaarOfferAccepted(
            listing_post_id="L-1",
            seller_user_id=seller.user_id,
            buyer_user_id=buyer.user_id,
            price=200,
        )
    )
    notifs = await stack.notif_repo.list(buyer.user_id, limit=10)
    assert any(n.type == "bazaar_offer_accepted" for n in notifs)


async def test_dm_contact_request_notifies_recipient(stack):
    from socialhome.domain.events import DmContactRequested

    recipient = await stack.provision_user("recipient")
    fake = _CapturingPush()
    stack.notif_svc.attach_push_service(fake)
    await stack.bus.publish(
        DmContactRequested(
            requester_user_id="u-other",
            requester_display_name="Outside Friend",
            recipient_user_id=recipient.user_id,
        )
    )
    notifs = await stack.notif_repo.list(recipient.user_id, limit=10)
    assert any(n.type == "dm_contact_requested" for n in notifs)
    assert fake.calls
    title = fake.calls[-1][1].title
    assert "Outside Friend" in title


# ─── CalendarEventCreated handler ──────────────────────────────────────


async def test_calendar_event_created_notifies_household(stack):
    from socialhome.domain.calendar import CalendarEvent
    from socialhome.domain.events import CalendarEventCreated

    alice = await stack.provision_user("alice-cal")
    bob = await stack.provision_user("bob-cal")
    event = CalendarEvent(
        id="e1",
        calendar_id="c1",
        summary="Team meeting",
        created_by=alice.user_id,
        start=datetime(2026, 5, 1, 10, tzinfo=timezone.utc),
        end=datetime(2026, 5, 1, 11, tzinfo=timezone.utc),
    )
    await stack.bus.publish(CalendarEventCreated(event=event))
    notifs = await stack.notif_repo.list(bob.user_id, limit=10)
    assert any(n.type == "calendar_event_created" for n in notifs)
    # Author should NOT be notified.
    author_notifs = await stack.notif_repo.list(alice.user_id, limit=10)
    assert not any(n.type == "calendar_event_created" for n in author_notifs)


# ─── TaskCompleted handler ─────────────────────────────────────────────


async def test_task_completed_notifies_assignees(stack):
    from socialhome.domain.events import TaskCompleted

    alice = await stack.provision_user("alice-tc")
    bob = await stack.provision_user("bob-tc")
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    task = Task(
        id="t1",
        list_id="l1",
        title="Buy milk",
        status=TaskStatus.DONE,
        position=0,
        created_by="me",
        created_at=now,
        updated_at=now,
        assignees=(bob.user_id,),
    )
    await stack.bus.publish(
        TaskCompleted(
            task=task,
            completed_by=alice.user_id,
        )
    )
    notifs = await stack.notif_repo.list(bob.user_id, limit=10)
    assert any(n.type == "task_completed" for n in notifs)


# ─── SpacePostModerated handler ───────────────────────────────────────


async def test_space_post_moderated_notifies_author(stack):
    from socialhome.domain.events import SpacePostModerated
    from socialhome.domain.post import Post, PostType

    author = await stack.provision_user("author-mod")
    post = Post(
        id="p-mod",
        author=author.user_id,
        type=PostType.TEXT,
        content="test",
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    await stack.bus.publish(
        SpacePostModerated(
            space_id="sp-1",
            post=post,
            moderated_by="admin",
        )
    )
    notifs = await stack.notif_repo.list(author.user_id, limit=10)
    assert any(n.type == "post_moderated" for n in notifs)


# ── Momentum (§Momentum) ──────────────────────────────────────────────


async def test_moment_reaction_notifies_author(stack):
    from socialhome.domain.events import MomentReactionChanged

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    await stack.bus.publish(
        MomentReactionChanged(
            moment_id="m-1",
            reactor_user_id=b.user_id,
            author_user_id=a.user_id,
            emoji="🔥",
        )
    )
    notifs = await stack.notif_repo.list(a.user_id, limit=10)
    assert any(n.type == "moment_reacted" and "🔥" in n.title for n in notifs)
    # bob (the reactor) gets nothing.
    assert await stack.notif_repo.list(b.user_id, limit=10) == []


async def test_moment_self_react_silent(stack):
    from socialhome.domain.events import MomentReactionChanged

    a = await stack.provision_user("anna")
    await stack.bus.publish(
        MomentReactionChanged(
            moment_id="m-1",
            reactor_user_id=a.user_id,
            author_user_id=a.user_id,
            emoji="❤️",
        )
    )
    assert await stack.notif_repo.list(a.user_id, limit=10) == []


async def test_moment_clear_reaction_silent(stack):
    """Clearing your reaction shouldn't ping the author again."""
    from socialhome.domain.events import MomentReactionChanged

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    await stack.bus.publish(
        MomentReactionChanged(
            moment_id="m-1",
            reactor_user_id=b.user_id,
            author_user_id=a.user_id,
            emoji=None,
        )
    )
    assert await stack.notif_repo.list(a.user_id, limit=10) == []


async def test_moment_reply_notifies_parent_author(stack):
    from socialhome.domain.events import MomentCreated

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    # Top-level moment from anna — does NOT notify anyone.
    await stack.bus.publish(
        MomentCreated(
            moment_id="m-root",
            author_user_id=a.user_id,
            content="root",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            parent_author_user_id=None,
            origin_instance_id="self",
            expires_at="2026-12-01T00:00:00+00:00",
        )
    )
    assert await stack.notif_repo.list(a.user_id, limit=10) == []
    # bob replies — anna gets the ping.
    await stack.bus.publish(
        MomentCreated(
            moment_id="m-reply",
            author_user_id=b.user_id,
            content="hey",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id="m-root",
            parent_author_user_id=a.user_id,
            origin_instance_id="self",
            expires_at="2026-12-01T00:00:00+00:00",
        )
    )
    notifs = await stack.notif_repo.list(a.user_id, limit=10)
    assert any(n.type == "moment_replied" for n in notifs)


async def test_user_followed_notifies_recipient(stack):
    from socialhome.domain.events import UserFollowed

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    await stack.bus.publish(
        UserFollowed(
            follower_user_id=b.user_id,
            followed_user_id=a.user_id,
        )
    )
    notifs = await stack.notif_repo.list(a.user_id, limit=10)
    assert any(n.type == "user_followed" for n in notifs)


async def test_user_self_follow_silent(stack):
    """Belt + braces — service-layer self-follow check is in user_service.
    The notification handler also short-circuits on self."""
    from socialhome.domain.events import UserFollowed

    a = await stack.provision_user("anna")
    await stack.bus.publish(
        UserFollowed(
            follower_user_id=a.user_id,
            followed_user_id=a.user_id,
        )
    )
    assert await stack.notif_repo.list(a.user_id, limit=10) == []
