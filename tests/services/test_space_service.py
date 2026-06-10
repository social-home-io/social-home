"""Tests for socialhome.services.space_service."""

from __future__ import annotations

import pytest

from socialhome.crypto import generate_identity_keypair, derive_instance_id
from socialhome.db.database import AsyncDatabase
from socialhome.domain.post import PostType
from socialhome.domain.space import (
    JoinMode,
    SpaceFeatureAccess,
    SpaceFeatures,
    SpacePermissionError,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.cp_repo import SqliteCpRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.child_protection_service import ChildProtectionService
from socialhome.services.space_service import SpaceService
from socialhome.services.user_service import UserService


@pytest.fixture
async def stack(tmp_dir):
    """Full service stack for space service tests."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        """INSERT INTO instance_identity(instance_id, identity_private_key,
           identity_public_key, routing_secret) VALUES(?,?,?,?)""",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    from socialhome.infrastructure.key_manager import KeyManager

    km = KeyManager(b"\x09" * 32)
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    space_repo = SqliteSpaceRepo(db, key_manager=km)
    space_post_repo = SqliteSpacePostRepo(db)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    space_svc = SpaceService(
        space_repo, space_post_repo, user_repo, bus, own_instance_id=iid
    )

    class Stack:
        pass

    s = Stack()
    s.db = db
    s.user_svc = user_svc
    s.space_svc = space_svc
    s.space_repo = space_repo
    s.space_post_repo = space_post_repo
    s.iid = iid
    s.km = km

    async def provision_user(username, **kw):
        return await user_svc.provision(username=username, display_name=username, **kw)

    s.provision_user = provision_user
    yield s
    await db.shutdown()


async def test_create_and_dissolve(stack):
    """Creating a space adds the owner as a member; dissolving removes the space."""
    _a = await stack.provision_user("anna", is_admin=True)
    space = await stack.space_svc.create_space(owner_username="anna", name="Family")
    assert space.name == "Family"
    members = await stack.space_repo.list_members(space.id)
    assert any(m.role == "owner" for m in members)
    await stack.space_svc.dissolve_space(space.id, actor_username="anna")
    with pytest.raises(KeyError):
        await stack.space_svc.list_feed(space.id)


async def test_member_management(stack):
    """add_member and remove_member adjust the member count correctly."""
    await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    members = await stack.space_repo.list_members(space.id)
    assert len(members) == 2
    await stack.space_svc.remove_member(
        space.id, actor_username="anna", user_id=b.user_id
    )
    members = await stack.space_repo.list_members(space.id)
    assert len(members) == 1


async def test_ban_and_unban(stack):
    """ban removes the member; unban clears the ban record."""
    _a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await stack.space_svc.ban(space.id, actor_username="anna", user_id=b.user_id)
    assert await stack.space_repo.is_banned(space.id, b.user_id)
    assert await stack.space_repo.get_member(space.id, b.user_id) is None
    await stack.space_svc.unban(space.id, actor_username="anna", user_id=b.user_id)
    assert not await stack.space_repo.is_banned(space.id, b.user_id)


async def test_invite_local_user_creates_pending_then_accept_seats(stack):
    """``invite_local_user`` creates a row in ``space_invitations``
    with status='pending'; the invitee is NOT yet a member. After
    ``accept_local_invite`` they're seated and the row flips to
    ``accepted``. Mirrors the §D1b cross-household flow Pascal
    asked for parity with."""
    await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    invitation_id = await stack.space_svc.invite_local_user(
        space.id,
        actor_username="anna",
        user_id=b.user_id,
    )
    pending = await stack.space_repo.list_pending_local_invites_for(b.user_id)
    assert any(r["id"] == invitation_id for r in pending)
    assert await stack.space_repo.get_member(space.id, b.user_id) is None
    member = await stack.space_svc.accept_local_invite(
        invitation_id,
        user_id=b.user_id,
    )
    assert member.user_id == b.user_id
    assert await stack.space_repo.get_member(space.id, b.user_id) is not None
    # Pending list is empty post-accept.
    assert await stack.space_repo.list_pending_local_invites_for(b.user_id) == []


async def test_invite_local_user_idempotent(stack):
    """Re-inviting the same user on the same space returns the
    existing pending row instead of stacking duplicates."""
    await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    first = await stack.space_svc.invite_local_user(
        space.id,
        actor_username="anna",
        user_id=b.user_id,
    )
    second = await stack.space_svc.invite_local_user(
        space.id,
        actor_username="anna",
        user_id=b.user_id,
    )
    assert first == second
    pending = await stack.space_repo.list_pending_local_invites_for(b.user_id)
    assert len(pending) == 1


async def test_invite_local_user_refuses_existing_member(stack):
    """Inviting a user who's already a member is a 409-shape error
    (SpacePermissionError) so the route can map it cleanly."""
    await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(
        space.id,
        actor_username="anna",
        user_id=b.user_id,
    )
    with pytest.raises(SpacePermissionError, match="already a member"):
        await stack.space_svc.invite_local_user(
            space.id,
            actor_username="anna",
            user_id=b.user_id,
        )


async def test_invite_local_user_refuses_banned_user(stack):
    """A banned user can't be invited; the invitee isn't given a
    prompt for a space they couldn't satisfy."""
    await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_repo.ban_member(
        space.id,
        b.user_id,
        banned_by="anna",
    )
    with pytest.raises(SpacePermissionError) as exc:
        await stack.space_svc.invite_local_user(
            space.id,
            actor_username="anna",
            user_id=b.user_id,
        )
    assert exc.value.banned is True


async def test_invite_local_user_requires_admin(stack):
    """Non-admin members can't invite — same gate as the existing
    add_member path."""
    await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    c = await stack.provision_user("carl")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(
        space.id,
        actor_username="anna",
        user_id=b.user_id,
    )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.invite_local_user(
            space.id,
            actor_username="bob",
            user_id=c.user_id,
        )


async def test_accept_local_invite_rejects_wrong_user(stack):
    """An invite addressed to bob cannot be accepted by carl."""
    await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    c = await stack.provision_user("carl")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    invitation_id = await stack.space_svc.invite_local_user(
        space.id,
        actor_username="anna",
        user_id=b.user_id,
    )
    with pytest.raises(SpacePermissionError, match="different user"):
        await stack.space_svc.accept_local_invite(
            invitation_id,
            user_id=c.user_id,
        )


async def test_accept_local_invite_unknown_id_raises(stack):
    """A bogus invitation id surfaces as KeyError so the route maps
    to 404."""
    await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    with pytest.raises(KeyError):
        await stack.space_svc.accept_local_invite(
            "no-such-id",
            user_id=b.user_id,
        )


async def test_decline_local_invite_marks_declined(stack):
    """Declining a pending invite flips status without seating the
    user; a second decline is a no-op."""
    await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    invitation_id = await stack.space_svc.invite_local_user(
        space.id,
        actor_username="anna",
        user_id=b.user_id,
    )
    await stack.space_svc.decline_local_invite(
        invitation_id,
        user_id=b.user_id,
    )
    assert await stack.space_repo.get_member(space.id, b.user_id) is None
    row = await stack.space_repo.get_invitation(invitation_id)
    assert row["status"] == "declined"
    # Idempotent — second decline doesn't toggle back to pending or
    # error out.
    await stack.space_svc.decline_local_invite(
        invitation_id,
        user_id=b.user_id,
    )
    row = await stack.space_repo.get_invitation(invitation_id)
    assert row["status"] == "declined"


async def test_accept_local_invite_refuses_already_accepted(stack):
    """Re-accepting a row that's already been accepted is a
    permission error — not a second seat."""
    await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    invitation_id = await stack.space_svc.invite_local_user(
        space.id,
        actor_username="anna",
        user_id=b.user_id,
    )
    await stack.space_svc.accept_local_invite(invitation_id, user_id=b.user_id)
    with pytest.raises(SpacePermissionError, match="already 'accepted'"):
        await stack.space_svc.accept_local_invite(
            invitation_id,
            user_id=b.user_id,
        )


async def test_local_invite_methods_refuse_remote_row(stack):
    """``accept_local_invite`` and ``decline_local_invite`` must
    refuse rows that belong to the cross-household flow — those go
    through ``/api/remote_invites/{token}/{decision}`` instead."""
    await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    invitation_id = await stack.space_repo.save_remote_invitation(
        space_id=space.id,
        invited_by="anna",
        remote_instance_id="peer-iid",
        remote_user_id=b.user_id,
        invite_token="tok-x",
    )
    with pytest.raises(SpacePermissionError, match="cross-household"):
        await stack.space_svc.accept_local_invite(
            invitation_id,
            user_id=b.user_id,
        )
    with pytest.raises(SpacePermissionError, match="cross-household"):
        await stack.space_svc.decline_local_invite(
            invitation_id,
            user_id=b.user_id,
        )


async def test_invite_flow(stack):
    """Invite token can be created and accepted; expired token is rejected."""
    _a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    tok = await stack.space_svc.create_invite_token(
        space.id, actor_username="anna", uses=1
    )
    m = await stack.space_svc.accept_invite_token(tok, user_id=b.user_id)
    assert m.role == "member"
    with pytest.raises(KeyError):
        await stack.space_svc.accept_invite_token(tok, user_id="uid-x")


async def test_set_role(stack):
    """set_role updates a member's role in the space."""
    _a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await stack.space_svc.set_role(
        space.id, actor_username="anna", user_id=b.user_id, role="admin"
    )
    m = await stack.space_repo.get_member(space.id, b.user_id)
    assert m.role == "admin"


async def test_non_owner_cannot_dissolve(stack):
    """Non-owner dissolving a space raises SpacePermissionError."""
    _a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.dissolve_space(space.id, actor_username="bob")


async def test_space_location_post_round_trip(stack):
    """Space-scoped location post: lat/lon truncated to 4dp at the
    service boundary, label preserved, post persisted."""
    from socialhome.domain.post import LocationData

    a = await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    p = await stack.space_svc.create_post(
        space.id,
        author_user_id=a.user_id,
        type=PostType.LOCATION,
        location=LocationData(lat=52.5200123456, lon=4.0600987, label="Marina"),
    )
    assert p is not None
    assert p.location is not None
    assert p.location.lat == 52.5200
    assert p.location.lon == 4.0601
    assert p.location.label == "Marina"


async def test_delete_space_post_removes_media_files(stack, tmp_dir):
    """Deleting a space image post unlinks its media file(s) from disk."""
    media_dir = tmp_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    svc = SpaceService(
        stack.space_repo,
        stack.space_post_repo,
        SqliteUserRepo(stack.db),
        EventBus(),
        own_instance_id=stack.iid,
        media_dir=media_dir,
    )
    a = await stack.provision_user("anna")
    space = await svc.create_space(owner_username="anna", name="S")
    (media_dir / "sp.webp").write_bytes(b"x")
    p = await svc.create_post(
        space.id,
        author_user_id=a.user_id,
        type=PostType.IMAGE,
        image_urls=["api/media/sp.webp"],
    )
    assert (media_dir / "sp.webp").exists()
    await svc.delete_post(p.id, actor_user_id=a.user_id)
    assert not (media_dir / "sp.webp").exists()


async def test_space_location_post_requires_coords(stack):
    """LOCATION without a LocationData payload is a 422 / ValueError."""
    a = await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    with pytest.raises(ValueError, match="lat/lon"):
        await stack.space_svc.create_post(
            space.id,
            author_user_id=a.user_id,
            type=PostType.LOCATION,
        )


async def test_space_location_post_label_capped(stack):
    """Label longer than LOCATION_LABEL_MAX (80) raises ValueError."""
    from socialhome.domain.post import LocationData

    a = await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    with pytest.raises(ValueError, match="label exceeds"):
        await stack.space_svc.create_post(
            space.id,
            author_user_id=a.user_id,
            type=PostType.LOCATION,
            location=LocationData(lat=10.0, lon=20.0, label="x" * 81),
        )


async def test_space_post_with_moderation(stack):
    """Moderated space queues regular member posts; admin posts go through directly."""
    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED),
    )
    result = await stack.space_svc.create_post(
        space.id,
        author_user_id=b.user_id,
        type=PostType.TEXT,
        content="pending",
    )
    assert result is None
    direct = await stack.space_svc.create_post(
        space.id,
        author_user_id=a.user_id,
        type=PostType.TEXT,
        content="admin ok",
    )
    assert direct is not None


async def test_approve_moderation_item_persists_post(stack):
    """Approving a queued post persists it and marks the queue item APPROVED."""
    from socialhome.domain.space import ModerationStatus

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED),
    )
    # Bob's post goes to the queue.
    assert (
        await stack.space_svc.create_post(
            space.id,
            author_user_id=b.user_id,
            type=PostType.TEXT,
            content="hello",
        )
        is None
    )
    pending = await stack.space_svc.list_pending_moderation(
        space.id,
        actor_username="anna",
    )
    assert len(pending) == 1
    approved_post = await stack.space_svc.approve_moderation_item(
        space.id,
        pending[0].id,
        actor_username="anna",
    )
    assert approved_post.content == "hello"
    assert approved_post.author == b.user_id
    # Item is now APPROVED; no longer listed as pending.
    assert (
        await stack.space_svc.list_pending_moderation(
            space.id,
            actor_username="anna",
        )
        == []
    )
    # The queued row should be loadable with its new status.
    item = await stack.space_svc._spaces.get_moderation_item(pending[0].id)
    assert item is not None and item.status is ModerationStatus.APPROVED
    assert item.reviewed_by == a.user_id


async def test_reject_moderation_item_records_reason(stack):
    from socialhome.domain.space import ModerationStatus

    _a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED),
    )
    await stack.space_svc.create_post(
        space.id,
        author_user_id=b.user_id,
        type=PostType.TEXT,
        content="spam",
    )
    pending = await stack.space_svc.list_pending_moderation(
        space.id,
        actor_username="anna",
    )
    await stack.space_svc.reject_moderation_item(
        space.id,
        pending[0].id,
        actor_username="anna",
        reason="off-topic",
    )
    item = await stack.space_svc._spaces.get_moderation_item(pending[0].id)
    assert item is not None
    assert item.status is ModerationStatus.REJECTED
    assert item.rejection_reason == "off-topic"


async def test_moderation_requires_admin(stack):
    _a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED),
    )
    await stack.space_svc.create_post(
        space.id,
        author_user_id=b.user_id,
        type=PostType.TEXT,
        content="x",
    )
    pending = await stack.space_svc.list_pending_moderation(
        space.id,
        actor_username="anna",
    )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.approve_moderation_item(
            space.id,
            pending[0].id,
            actor_username="bob",
        )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.list_pending_moderation(
            space.id,
            actor_username="bob",
        )


async def test_double_decide_raises_already_decided(stack):
    from socialhome.domain.space import ModerationAlreadyDecidedError

    _a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED),
    )
    await stack.space_svc.create_post(
        space.id,
        author_user_id=b.user_id,
        type=PostType.TEXT,
        content="x",
    )
    pending = await stack.space_svc.list_pending_moderation(
        space.id,
        actor_username="anna",
    )
    await stack.space_svc.approve_moderation_item(
        space.id,
        pending[0].id,
        actor_username="anna",
    )
    with pytest.raises(ModerationAlreadyDecidedError):
        await stack.space_svc.approve_moderation_item(
            space.id,
            pending[0].id,
            actor_username="anna",
        )
    with pytest.raises(ModerationAlreadyDecidedError):
        await stack.space_svc.reject_moderation_item(
            space.id,
            pending[0].id,
            actor_username="anna",
        )


async def test_space_post_admin_only(stack):
    """ADMIN_ONLY space rejects regular member posts with SpacePermissionError."""
    _a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(posts_access=SpaceFeatureAccess.ADMIN_ONLY),
    )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.create_post(
            space.id,
            author_user_id=b.user_id,
            type=PostType.TEXT,
            content="denied",
        )


async def test_transfer_ownership(stack):
    """Transferring ownership makes the new owner's role 'owner' and demotes the old one."""
    anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="Family")
    await stack.space_svc.add_member(
        space.id, actor_username="anna", user_id=bob.user_id
    )
    await stack.space_svc.transfer_ownership(
        space.id,
        actor_username="anna",
        to_user_id=bob.user_id,
    )
    anna_member = await stack.space_repo.get_member(space.id, anna.user_id)
    bob_member = await stack.space_repo.get_member(space.id, bob.user_id)
    assert bob_member.role == "owner"
    assert anna_member.role == "admin"


async def test_join_request_approve(stack):
    """Open space: request to join, then admin approves, user becomes a member."""
    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="Open",
        join_mode=JoinMode.OPEN,
    )
    req_id = await stack.space_svc.request_join(space.id, user_id=bob.user_id)
    member = await stack.space_svc.approve_join_request(req_id, actor_username="anna")
    assert member.user_id == bob.user_id
    assert member.role == "member"


async def test_join_request_deny(stack):
    """Denied join request does not add the user to the space."""
    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="Open",
        join_mode=JoinMode.OPEN,
    )
    req_id = await stack.space_svc.request_join(space.id, user_id=bob.user_id)
    await stack.space_svc.deny_join_request(req_id, actor_username="anna")
    members = await stack.space_repo.list_members(space.id)
    assert bob.user_id not in {m.user_id for m in members}


async def test_invite_only_rejects_join_request(stack):
    """Invite-only space rejects join requests with SpacePermissionError."""
    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="Private",
        join_mode=JoinMode.INVITE_ONLY,
    )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.request_join(space.id, user_id=bob.user_id)


async def test_update_config_branches(stack):
    """update_config handles name, description+emoji, features, join_mode, retention."""
    _anna = await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="Original")

    updated = await stack.space_svc.update_config(
        space.id, actor_username="anna", name="Renamed"
    )
    assert updated.name == "Renamed"

    updated2 = await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        description="A great space",
        emoji="🏠",
    )
    assert updated2.description == "A great space"

    new_features = SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED)
    updated3 = await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=new_features,
    )
    assert updated3.features.posts_access == SpaceFeatureAccess.MODERATED

    updated4 = await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        join_mode=JoinMode.OPEN,
    )
    assert updated4.join_mode == JoinMode.OPEN

    updated5 = await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        retention_days=30,
    )
    assert updated5.retention_days == 30

    updated6 = await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        retention_days=0,
    )
    assert updated6.retention_days is None


async def test_update_config_persists_delegated_admin_authority(stack):
    """update_config with features enabling delegated_admin_authority persists
    and reloads True (Phase 1a flag plumbing — no key-share behaviour yet)."""
    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="Deleg")
    # Default is OFF on a fresh space.
    assert space.features.delegated_admin_authority is False

    updated = await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(delegated_admin_authority=True),
    )
    assert updated.features.delegated_admin_authority is True

    reloaded = await stack.space_repo.get(space.id)
    assert reloaded is not None
    assert reloaded.features.delegated_admin_authority is True


async def test_delegated_admin_authority_flip_is_owner_only(stack):
    """Toggling delegated_admin_authority is OWNER-only — a non-owner local
    admin cannot enact the owner's delegation policy (which distributes the
    space signing seed). Other config edits by that admin still work."""
    from socialhome.domain.space import SpaceRole

    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="Deleg")
    await stack.space_svc.add_member(
        space.id, actor_username="anna", user_id=bob.user_id
    )
    await stack.space_svc.set_role(
        space.id, actor_username="anna", user_id=bob.user_id, role=SpaceRole.ADMIN
    )

    # Admin bob may edit normal config…
    await stack.space_svc.update_config(
        space.id, actor_username="bob", description="bob edited"
    )
    # …but must NOT be able to flip delegated_admin_authority on.
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.update_config(
            space.id,
            actor_username="bob",
            features=SpaceFeatures(delegated_admin_authority=True),
        )
    reloaded = await stack.space_repo.get(space.id)
    assert reloaded is not None
    assert reloaded.features.delegated_admin_authority is False


async def _seat_remote_delegated_space(stack, *, actor, seed=None):
    """Create a stub for a space hosted ELSEWHERE with delegated_admin_authority
    ON, seat ``actor`` locally as ADMIN, and (optionally) store a space seed.
    Returns the space id."""
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceMember,
        SpaceRole,
        SpaceType,
    )

    actor_user = await stack.provision_user(actor)
    space = Space(
        id="sp-remote-deleg",
        name="Remote",
        owner_instance_id="inst-remote-owner",  # hosted elsewhere
        owner_username="remoteowner",
        identity_public_key="aa" * 32,
        config_sequence=3,
        features=SpaceFeatures(delegated_admin_authority=True),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )
    await stack.space_repo.save(space)
    await stack.space_repo.save_member(
        SpaceMember(
            space_id=space.id,
            user_id=actor_user.user_id,
            role=SpaceRole.ADMIN,
            joined_at="2025-01-01T00:00:00",
        )
    )
    if seed is not None:
        await stack.space_repo.set_space_seed(space.id, seed)
    return space.id


async def test_delegated_admin_with_seed_executes_locally(stack):
    """v_24: a seed-holding delegated admin editing a REMOTE-owned space with
    delegation ON executes the edit LOCALLY (no forward) and broadcasts an
    authority-signed SPACE_CONFIG_CHANGED that verifies against the space key."""
    from unittest.mock import AsyncMock, MagicMock

    from socialhome.crypto import generate_space_keypair
    from socialhome.domain.federation import FederationEventType
    from socialhome.services.space_crypto_service import (
        strip_authority_sig_fields,
        verify_authority_event,
    )

    kp = generate_space_keypair()
    sid = await _seat_remote_delegated_space(stack, actor="anna", seed=kp.private_key)
    # Replace the stub's pubkey with the seed's real public half so the
    # broadcast signature verifies.
    await stack.space_repo.set_space_pubkey(sid, kp.public_key.hex())

    fed = MagicMock()
    fed._own_instance_id = stack.iid
    fed.broadcast_to_space_members = AsyncMock()
    fed.peer_supports = AsyncMock(return_value=True)
    fed.send_with_mesh_fallback = AsyncMock()
    stack.space_svc._federation = fed
    # Wire the outbound so the bus event turns into a federation broadcast.
    from socialhome.services.space_config_outbound import SpaceConfigOutbound

    SpaceConfigOutbound(
        bus=stack.space_svc._bus,
        federation_service=fed,
        space_repo=stack.space_repo,
    ).wire()

    await stack.space_svc.update_config(sid, actor_username="anna", name="LocalEdit")

    # NOT forwarded as a remote-admin action.
    forwarded = [
        c
        for c in fed.broadcast_to_space_members.await_args_list
        if c.args[1] is FederationEventType.SPACE_REMOTE_ADMIN_ACTION
    ]
    assert forwarded == []
    # Applied locally + sequence bumped.
    reloaded = await stack.space_repo.get(sid)
    assert reloaded.name == "LocalEdit"
    assert reloaded.config_sequence == 4
    # An authority-signed SPACE_CONFIG_CHANGED went out and verifies.
    cfg = [
        c
        for c in fed.broadcast_to_space_members.await_args_list
        if c.args[1] is FederationEventType.SPACE_CONFIG_CHANGED
    ]
    assert len(cfg) == 1
    meta = cfg[0].args[2]["space_meta"]
    assert verify_authority_event(
        event_type="space_config_changed",
        space_id=sid,
        payload=strip_authority_sig_fields(meta),
        authority_sig=meta["authority_sig"],
        authority_sig_suite=meta["authority_sig_suite"],
        space_public_key=kp.public_key,
    )


async def test_delegated_admin_without_seed_forwards(stack):
    """Delegation ON but NO seed held → keep today's forward-to-host behaviour
    (Phase 6 gates that behind owner approval). No local authoritative edit."""
    from unittest.mock import AsyncMock, MagicMock

    sid = await _seat_remote_delegated_space(stack, actor="anna", seed=None)

    fed = MagicMock()
    fed._own_instance_id = stack.iid
    fed.peer_supports = AsyncMock(return_value=True)
    fed.send_with_mesh_fallback = AsyncMock()
    fed.broadcast_to_space_members = AsyncMock()
    stack.space_svc._federation = fed

    await stack.space_svc.update_config(sid, actor_username="anna", name="Forwarded")

    # The forward path ships a SPACE_REMOTE_ADMIN_ACTION to the host…
    assert fed.send_with_mesh_fallback.await_count >= 1
    # …and the local stub is NOT authoritatively mutated by us.
    reloaded = await stack.space_repo.get(sid)
    assert reloaded.name == "Remote"
    assert reloaded.config_sequence == 3


@pytest.mark.security
@pytest.mark.parametrize("tier", ["public", "global"])
async def test_delegated_admin_local_execute_rejects_space_type(stack, tier):
    """SECURITY (Defect 1): the v_24 delegated-admin local-execute path must NOT
    apply a publication-tier (space_type) change locally — tier changes are
    owner/quorum-gated (v_16) and the v_15 forward path deliberately excludes
    space_type. A seed-holding delegated admin flipping PRIVATE→PUBLIC/GLOBAL
    must be rejected, not executed locally with zero quorum."""
    from socialhome.crypto import generate_space_keypair

    kp = generate_space_keypair()
    sid = await _seat_remote_delegated_space(stack, actor="anna", seed=kp.private_key)
    await stack.space_repo.set_space_pubkey(sid, kp.public_key.hex())

    before = await stack.space_repo.get(sid)
    assert before.space_type is SpaceType.PRIVATE
    assert before.config_sequence == 3

    with pytest.raises(SpacePermissionError, match="publication tier"):
        await stack.space_svc.update_config(sid, actor_username="anna", space_type=tier)

    # Tier UNCHANGED and no local config bump leaked.
    after = await stack.space_repo.get(sid)
    assert after.space_type is SpaceType.PRIVATE
    assert after.config_sequence == 3


async def test_delegated_admin_local_execute_seq_author_recorded(stack):
    """Defect 2 (unit half): the v_24 local authoritative edit must record THIS
    household as the last-applied config author, matching what every receiver
    records from the signed payload — otherwise the editing admin's LWW
    tie-break key diverges from clean members'."""
    from socialhome.crypto import generate_space_keypair

    kp = generate_space_keypair()
    sid = await _seat_remote_delegated_space(stack, actor="anna", seed=kp.private_key)
    await stack.space_repo.set_space_pubkey(sid, kp.public_key.hex())

    await stack.space_svc.update_config(sid, actor_username="anna", name="LocalEdit")

    # The editing household recorded ITSELF as the config author.
    assert await stack.space_repo.get_config_author(sid) == stack.iid


async def test_delegated_admin_local_edit_converges_with_member(stack, tmp_dir):
    """SECURITY (Defect 2): a seed-holding delegated admin does a local edit
    reaching seq=N (admin as author), then ingests a concurrent peer's
    authority-signed SPACE_CONFIG_CHANGED at the SAME seq=N from a DIFFERENT
    author. The editing admin's row and a clean member household must converge
    to the SAME deterministic (seq, author) winner — regardless of which author
    sorts higher. Before the fix the admin's NULL author fell back to
    owner_instance_id, mis-ordering the tie-break and diverging permanently."""
    from socialhome.crypto import generate_space_keypair
    from socialhome.db.database import AsyncDatabase
    from socialhome.domain.federation import FederationEvent, FederationEventType
    from socialhome.infrastructure.event_bus import EventBus
    from socialhome.repositories.conversation_repo import SqliteConversationRepo
    from socialhome.repositories.user_repo import SqliteUserRepo
    from socialhome.services.federation_inbound_service import (
        FederationInboundService,
    )
    from socialhome.services.space_crypto_service import (
        sign_authority_event,
        strip_authority_sig_fields,
    )
    from datetime import datetime, timezone

    from dataclasses import replace as _replace

    kp = generate_space_keypair()
    sid = await _seat_remote_delegated_space(stack, actor="anna", seed=kp.private_key)
    await stack.space_repo.set_space_pubkey(sid, kp.public_key.hex())

    # The bug bites in the window stack.iid < peer_author < owner_instance_id:
    # the admin's NULL author falls back to owner_instance_id and DROPS the peer
    # edit, while a clean member (which recorded the admin's real iid) ACCEPTS
    # it. Pin a deterministic owner id strictly above the chosen peer_author so
    # the window exists regardless of the random stack.iid.
    owner_id = "z" * 40
    peer_author = stack.iid + "0"  # strictly > stack.iid (prefix extension)
    assert stack.iid < peer_author < owner_id

    # 1) Admin does a LOCAL authoritative edit → reaches seq=4, author=stack.iid.
    await stack.space_svc.update_config(sid, actor_username="anna", name="ByAdmin")
    assert (await stack.space_repo.get(sid)).config_sequence == 4
    # Pin a deterministic owner id strictly above the chosen peer_author AFTER
    # the local edit (``update_config`` re-saves the row from the snapshot it
    # read at entry, which would otherwise revert this). This guarantees the
    # divergence window exists regardless of the random stack.iid.
    edited = await stack.space_repo.get(sid)
    await stack.space_repo.save(_replace(edited, owner_instance_id=owner_id))

    # 2) A concurrent peer edit at the SAME seq=4 from a DIFFERENT author.
    def _signed_peer_event(space_id):
        meta = {
            "name": "ByPeer",
            "owner_instance_id": owner_id,
            "owner_username": "remoteowner",
            "identity_public_key": "ignored-by-stub",
            "config_sequence": 4,
            "config_author_instance": peer_author,
            "space_type": "private",
            "join_mode": "invite_only",
            "features": SpaceFeatures(delegated_admin_authority=True).to_wire_dict(),
        }
        signed = sign_authority_event(
            event_type="space_config_changed",
            space_id=space_id,
            payload=strip_authority_sig_fields(meta),
            space_seed=kp.private_key,
        )
        meta.update(signed)
        return FederationEvent(
            msg_id="msg-peer",
            event_type=FederationEventType.SPACE_CONFIG_CHANGED,
            from_instance=peer_author,
            to_instance="self",
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload={
                "space_id": space_id,
                "sequence": 4,
                "event_type": "rename",
                "space_meta": meta,
            },
            space_id=space_id,
        )

    # Editing admin ingests the concurrent peer edit.
    admin_inbound = FederationInboundService(
        bus=stack.space_svc._bus,
        conversation_repo=SqliteConversationRepo(stack.db),
        space_post_repo=stack.space_post_repo,
        space_repo=stack.space_repo,
        user_repo=SqliteUserRepo(stack.db),
    )
    await admin_inbound._on_space_config_changed(_signed_peer_event(sid))
    admin_final = await stack.space_repo.get(sid)

    # 3) A clean member household: same starting stub (seq=3), applies BOTH the
    # admin's edit (seq=4, author=stack.iid) and the peer's (seq=4, peer_author).
    db2 = AsyncDatabase(tmp_dir / "member.db", batch_timeout_ms=10)
    await db2.startup()
    from socialhome.infrastructure.key_manager import KeyManager

    member_repo = SqliteSpaceRepo(db2, key_manager=KeyManager(b"\x07" * 32))
    member_inbound = FederationInboundService(
        bus=EventBus(),
        conversation_repo=SqliteConversationRepo(db2),
        space_post_repo=SqliteSpacePostRepo(db2),
        space_repo=member_repo,
        user_repo=SqliteUserRepo(db2),
    )
    await db2.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode,
                              config_sequence)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            sid,
            "Remote",
            owner_id,
            "remoteowner",
            kp.public_key.hex(),
            SpaceType.PRIVATE.value,
            JoinMode.INVITE_ONLY.value,
            3,
        ),
    )

    def _signed_admin_event(space_id):
        meta = {
            "name": "ByAdmin",
            "owner_instance_id": owner_id,
            "owner_username": "remoteowner",
            "identity_public_key": "ignored-by-stub",
            "config_sequence": 4,
            "config_author_instance": stack.iid,
            "space_type": "private",
            "join_mode": "invite_only",
            "features": SpaceFeatures(delegated_admin_authority=True).to_wire_dict(),
        }
        signed = sign_authority_event(
            event_type="space_config_changed",
            space_id=space_id,
            payload=strip_authority_sig_fields(meta),
            space_seed=kp.private_key,
        )
        meta.update(signed)
        return FederationEvent(
            msg_id="msg-admin",
            event_type=FederationEventType.SPACE_CONFIG_CHANGED,
            from_instance=stack.iid,
            to_instance="self",
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload={
                "space_id": space_id,
                "sequence": 4,
                "event_type": "rename",
                "space_meta": meta,
            },
            space_id=space_id,
        )

    # Clean member sees both edits (peer first, then admin's).
    await member_inbound._on_space_config_changed(_signed_peer_event(sid))
    await member_inbound._on_space_config_changed(_signed_admin_event(sid))
    member_final = await member_repo.get(sid)
    await db2.shutdown()

    # CONVERGENCE: the editing admin and a clean member agree on the winner.
    # Deterministic (seq, author): peer_author > stack.iid, so "ByPeer" wins on
    # BOTH. Before the fix the admin's NULL→owner_id fallback (owner_id >
    # peer_author) wrongly dropped the peer edit, leaving the admin on "ByAdmin"
    # while the clean member converged on "ByPeer" — permanent divergence.
    assert admin_final.name == member_final.name
    assert admin_final.name == "ByPeer"
    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="Exempt",
    )
    updated = await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        retention_exempt_types=["list", "poll", "", "  ", "schedule"],
    )
    # Empty / whitespace entries stripped; rest preserved as a tuple.
    assert updated.retention_exempt_types == ("list", "poll", "schedule")


# ─── Delegated-admin ban/remove offline-of-owner (Phase 4b) ──────────────


async def _seat_target_member(stack, sid, *, username, role=None):
    """Seat ``username`` as a member of the (remote-owned) stub space ``sid``
    so the delegated admin has someone local to remove/ban. Returns the user."""
    from socialhome.domain.space import SpaceMember, SpaceRole

    user = await stack.provision_user(username)
    await stack.space_repo.save_member(
        SpaceMember(
            space_id=sid,
            user_id=user.user_id,
            role=role or SpaceRole.MEMBER,
            joined_at="2025-01-02T00:00:00",
        )
    )
    return user


def _wire_local_fed_and_crypto(stack):
    """Wire a MagicMock federation + AsyncMock crypto onto the stack so a
    delegated-admin local-execute path can broadcast + rotate. Returns
    ``(fed, crypto)``."""
    from unittest.mock import AsyncMock, MagicMock

    fed = MagicMock()
    fed._own_instance_id = stack.iid
    fed.broadcast_to_space_members = AsyncMock()
    fed.peer_supports = AsyncMock(return_value=True)
    fed.send_with_mesh_fallback = AsyncMock()
    crypto = AsyncMock()
    crypto.rotate_epoch = AsyncMock(return_value=9)
    crypto.export_current_key = AsyncMock(return_value=(9, bytes(range(32))))
    stack.space_svc._federation = fed
    stack.space_svc.attach_space_crypto_service(crypto)
    return fed, crypto


async def test_delegated_admin_ban_offline_executes_locally(stack):
    """Phase 4b: a seed-holding delegated admin (delegation ON) bans a member
    while the owner is offline → the ban is applied LOCALLY (not forwarded), an
    authority-signed SPACE_MEMBER_LEFT gossip fires (verifies against the space
    pubkey), AND the forward-secret rekey rotation runs."""
    from socialhome.crypto import generate_space_keypair
    from socialhome.domain.federation import FederationEventType
    from socialhome.services.space_crypto_service import (
        strip_authority_sig_fields,
        verify_authority_event,
    )

    kp = generate_space_keypair()
    sid = await _seat_remote_delegated_space(stack, actor="anna", seed=kp.private_key)
    await stack.space_repo.set_space_pubkey(sid, kp.public_key.hex())
    target = await _seat_target_member(stack, sid, username="bob")
    fed, crypto = _wire_local_fed_and_crypto(stack)

    await stack.space_svc.ban(sid, actor_username="anna", user_id=target.user_id)

    # NOT forwarded as a remote-admin action.
    assert fed.send_with_mesh_fallback.await_count == 0
    # Member is tombstoned + banned locally.
    assert await stack.space_repo.get_member(sid, target.user_id) is None
    assert await stack.space_repo.is_banned(sid, target.user_id)
    # An authority-signed SPACE_MEMBER_LEFT went out + verifies against the space.
    left = [
        c
        for c in fed.broadcast_to_space_members.await_args_list
        if c.args[1] is FederationEventType.SPACE_MEMBER_LEFT
    ]
    assert len(left) == 1
    p = left[0].args[2]
    assert verify_authority_event(
        event_type="space_member_left",
        space_id=sid,
        payload=strip_authority_sig_fields(p),
        authority_sig=p["authority_sig"],
        authority_sig_suite=p["authority_sig_suite"],
        space_public_key=kp.public_key,
    )
    # Forward-secret rekey ran.
    crypto.rotate_epoch.assert_awaited_once_with(sid)
    assert any(
        c.args[1] is FederationEventType.SPACE_KEY_EXCHANGE_REKEY
        for c in fed.broadcast_to_space_members.await_args_list
    )


async def test_delegated_admin_remove_offline_executes_locally(stack):
    """Phase 4b: remove_member mirrors ban — a seed-holding delegated admin
    removes a member locally (no SPACE_REMOTE_ADMIN_KICK forward) + rotates."""
    from socialhome.crypto import generate_space_keypair
    from socialhome.domain.federation import FederationEventType

    kp = generate_space_keypair()
    sid = await _seat_remote_delegated_space(stack, actor="anna", seed=kp.private_key)
    await stack.space_repo.set_space_pubkey(sid, kp.public_key.hex())
    target = await _seat_target_member(stack, sid, username="bob")
    fed, crypto = _wire_local_fed_and_crypto(stack)

    await stack.space_svc.remove_member(
        sid, actor_username="anna", user_id=target.user_id
    )

    # NOT forwarded as a remote-admin kick.
    kicks = [
        c
        for c in fed.send_with_mesh_fallback.await_args_list
        if c.kwargs.get("event_type") is FederationEventType.SPACE_REMOTE_ADMIN_KICK
    ]
    assert kicks == []
    assert await stack.space_repo.get_member(sid, target.user_id) is None
    crypto.rotate_epoch.assert_awaited_once_with(sid)


async def test_delegated_admin_ban_without_seed_forwards_no_rotation(stack):
    """Delegation ON but NO seed held → ban forwards to the host (v_15) and does
    NOT rotate locally (the host owns the authoritative rotation)."""
    from unittest.mock import AsyncMock, MagicMock

    sid = await _seat_remote_delegated_space(stack, actor="anna", seed=None)
    target = await _seat_target_member(stack, sid, username="bob")

    fed = MagicMock()
    fed._own_instance_id = stack.iid
    fed.peer_supports = AsyncMock(return_value=True)
    fed.send_with_mesh_fallback = AsyncMock()
    fed.broadcast_to_space_members = AsyncMock()
    crypto = AsyncMock()
    crypto.rotate_epoch = AsyncMock(return_value=1)
    stack.space_svc._federation = fed
    stack.space_svc.attach_space_crypto_service(crypto)

    await stack.space_svc.ban(sid, actor_username="anna", user_id=target.user_id)

    # Forwarded; the local stub is NOT authoritatively mutated by us.
    assert fed.send_with_mesh_fallback.await_count >= 1
    crypto.rotate_epoch.assert_not_awaited()


async def test_delegated_admin_remove_tombstones_before_rotate(stack):
    """Forward secrecy ordering: on the delegated local-remove path the removed
    member is gone from ``space_members`` BEFORE the rekey rotation runs, so the
    member who lost access can't be counted into the new-key fan-out. Assert the
    member row is already deleted at the moment ``rotate_epoch`` is invoked."""
    from socialhome.crypto import generate_space_keypair
    from socialhome.domain.federation import FederationEventType

    kp = generate_space_keypair()
    sid = await _seat_remote_delegated_space(stack, actor="anna", seed=kp.private_key)
    await stack.space_repo.set_space_pubkey(sid, kp.public_key.hex())
    target = await _seat_target_member(stack, sid, username="bob")
    fed, crypto = _wire_local_fed_and_crypto(stack)

    gone_at_rotate = {}

    async def _capture(space_id):
        gone_at_rotate["bob_present"] = (
            await stack.space_repo.get_member(space_id, target.user_id) is not None
        )
        return 9

    crypto.rotate_epoch.side_effect = _capture

    await stack.space_svc.remove_member(
        sid, actor_username="anna", user_id=target.user_id
    )
    # Bob was already tombstoned by the time the rekey rotation ran.
    assert gone_at_rotate["bob_present"] is False
    assert any(
        c.args[1] is FederationEventType.SPACE_KEY_EXCHANGE_REKEY
        for c in fed.broadcast_to_space_members.await_args_list
    )


async def test_public_space_requires_coordinates(stack):
    """Creating a public space without lat/lon raises ValueError."""
    await stack.provision_user("a")
    with pytest.raises(ValueError, match="lat"):
        await stack.space_svc.create_space(
            owner_username="a",
            name="Pub",
            space_type=SpaceType.PUBLIC,
            join_mode=JoinMode.OPEN,
        )


async def test_public_space_with_coordinates(stack):
    """Public space stores 4dp-truncated coordinates."""
    await stack.provision_user("a")
    s = await stack.space_svc.create_space(
        owner_username="a",
        name="Pub",
        space_type=SpaceType.PUBLIC,
        join_mode=JoinMode.OPEN,
        lat=52.376543,
        lon=4.895678,
        radius_km=5.0,
    )
    assert s.lat == 52.3765 and s.lon == 4.8957


async def test_non_member_cannot_post(stack):
    """Non-member posting raises SpacePermissionError."""
    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.create_post(
            space.id,
            author_user_id=bob.user_id,
            type=PostType.TEXT,
            content="Unauthorised post",
        )


async def test_pin_unpin_alias(stack):
    """Sidebar pin, unpin, and space alias operations complete without error."""
    anna = await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.pin(anna.user_id, space.id, position=1)
    await stack.space_svc.unpin(anna.user_id, space.id)
    await stack.space_svc.set_alias(space.id, username="anna", alias="home")
    assert True


# ─── Space post CRUD edge paths ──────────────────────────────────────────


async def test_space_edit_post_nonexistent(stack):
    """Editing a nonexistent space post raises KeyError."""
    with pytest.raises(KeyError):
        await stack.space_svc.edit_post("nope", editor_user_id="u", new_content="x")


async def test_space_edit_post_author_allowed(stack):
    """Author can edit their own space post."""
    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(s.id, actor_username="anna", user_id=bob.user_id)
    p = await stack.space_svc.create_post(
        s.id, author_user_id=bob.user_id, type=PostType.TEXT, content="v1"
    )
    updated = await stack.space_svc.edit_post(
        p.id, editor_user_id=bob.user_id, new_content="v2"
    )
    assert updated.content == "v2"


async def test_space_edit_post_non_admin_rejected(stack):
    """Non-author non-admin editing raises PermissionError."""
    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    carl = await stack.provision_user("carl")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(s.id, actor_username="anna", user_id=bob.user_id)
    await stack.space_svc.add_member(s.id, actor_username="anna", user_id=carl.user_id)
    p = await stack.space_svc.create_post(
        s.id, author_user_id=bob.user_id, type=PostType.TEXT, content="x"
    )
    with pytest.raises(PermissionError):
        await stack.space_svc.edit_post(
            p.id, editor_user_id=carl.user_id, new_content="y"
        )


async def test_space_delete_post_self_no_moderated_flag(stack):
    """Self-deleting a space post does not set moderated flag."""
    anna = await stack.provision_user("anna")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    p = await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="x"
    )
    await stack.space_svc.delete_post(p.id, actor_user_id=anna.user_id)
    got = (await stack.space_post_repo.get(p.id))[1]
    assert got.deleted and not got.moderated


async def test_space_delete_post_admin_sets_moderated(stack):
    """Admin deleting another's post sets moderated flag."""
    anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(s.id, actor_username="anna", user_id=bob.user_id)
    p = await stack.space_svc.create_post(
        s.id, author_user_id=bob.user_id, type=PostType.TEXT, content="x"
    )
    await stack.space_svc.delete_post(p.id, actor_user_id=anna.user_id)
    got = (await stack.space_post_repo.get(p.id))[1]
    assert got.deleted and got.moderated


async def test_space_delete_post_nonexistent(stack):
    """Deleting a nonexistent post raises KeyError."""
    with pytest.raises(KeyError):
        await stack.space_svc.delete_post("nope", actor_user_id="u")


async def test_space_delete_post_non_admin_rejected(stack):
    """Non-author non-admin cannot delete another's post."""
    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    carl = await stack.provision_user("carl")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(s.id, actor_username="anna", user_id=bob.user_id)
    await stack.space_svc.add_member(s.id, actor_username="anna", user_id=carl.user_id)
    p = await stack.space_svc.create_post(
        s.id, author_user_id=bob.user_id, type=PostType.TEXT, content="x"
    )
    with pytest.raises(PermissionError):
        await stack.space_svc.delete_post(p.id, actor_user_id=carl.user_id)


async def test_space_reactions(stack):
    """Add and remove reaction on a space post."""
    anna = await stack.provision_user("anna")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    p = await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="x"
    )
    r = await stack.space_svc.add_reaction(p.id, user_id=anna.user_id, emoji=" 👍 ")
    assert "👍" in r.reactions
    r2 = await stack.space_svc.remove_reaction(p.id, user_id=anna.user_id, emoji="👍")
    assert "👍" not in r2.reactions


async def test_space_reaction_empty_rejected(stack):
    """Empty emoji raises ValueError."""
    anna = await stack.provision_user("anna")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    p = await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="x"
    )
    with pytest.raises(ValueError, match="empty"):
        await stack.space_svc.add_reaction(p.id, user_id=anna.user_id, emoji="")


async def test_space_comment_and_delete(stack):
    """Add comment, then admin deletes it."""
    anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(s.id, actor_username="anna", user_id=bob.user_id)
    p = await stack.space_svc.create_post(
        s.id, author_user_id=bob.user_id, type=PostType.TEXT, content="x"
    )
    c = await stack.space_svc.add_comment(
        p.id, author_user_id=bob.user_id, content="nice"
    )
    await stack.space_svc.delete_comment(c.id, actor_user_id=anna.user_id)
    got = await stack.space_post_repo.get_comment(c.id)
    assert got.deleted


async def test_space_comment_non_member_rejected(stack):
    """Non-member cannot comment on a space post."""
    anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    p = await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="x"
    )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.add_comment(
            p.id, author_user_id=bob.user_id, content="nope"
        )


async def test_space_comment_on_deleted_post(stack):
    """Commenting on a deleted post raises KeyError."""
    anna = await stack.provision_user("anna")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    p = await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="x"
    )
    await stack.space_svc.delete_post(p.id, actor_user_id=anna.user_id)
    with pytest.raises(KeyError, match="deleted"):
        await stack.space_svc.add_comment(
            p.id, author_user_id=anna.user_id, content="late"
        )


async def test_space_comment_empty_content_rejected(stack):
    """Empty comment content raises ValueError."""
    anna = await stack.provision_user("anna")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    p = await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="x"
    )
    with pytest.raises(ValueError, match="content"):
        await stack.space_svc.add_comment(
            p.id, author_user_id=anna.user_id, content="  "
        )


async def test_space_delete_comment_nonexistent(stack):
    """Deleting a nonexistent comment raises KeyError."""
    with pytest.raises(KeyError):
        await stack.space_svc.delete_comment("nope", actor_user_id="u")


async def test_space_delete_comment_non_admin_rejected(stack):
    """Non-author non-admin cannot delete someone else's comment."""
    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    carl = await stack.provision_user("carl")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(s.id, actor_username="anna", user_id=bob.user_id)
    await stack.space_svc.add_member(s.id, actor_username="anna", user_id=carl.user_id)
    p = await stack.space_svc.create_post(
        s.id, author_user_id=bob.user_id, type=PostType.TEXT, content="x"
    )
    c = await stack.space_svc.add_comment(
        p.id, author_user_id=bob.user_id, content="hi"
    )
    with pytest.raises(PermissionError):
        await stack.space_svc.delete_comment(c.id, actor_user_id=carl.user_id)


async def test_space_list_feed(stack):
    """list_feed returns posts scoped to the space."""
    anna = await stack.provision_user("anna")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="a"
    )
    await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="b"
    )
    feed = await stack.space_svc.list_feed(s.id, limit=10)
    assert len(feed) == 2


async def test_space_create_post_type_not_allowed(stack):
    """Posting a disallowed type raises SpacePermissionError."""
    anna = await stack.provision_user("anna")
    s = await stack.space_svc.create_space(
        owner_username="anna",
        name="S",
        features=SpaceFeatures(allowed_post_types=("text",)),
    )
    with pytest.raises(SpacePermissionError, match="does not allow"):
        await stack.space_svc.create_post(
            s.id,
            author_user_id=anna.user_id,
            type="image",
            media_url="/img.webp",
        )


async def test_space_create_post_text_empty_rejected(stack):
    """Text post with empty content raises ValueError."""
    anna = await stack.provision_user("anna")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    with pytest.raises(ValueError, match="content"):
        await stack.space_svc.create_post(
            s.id,
            author_user_id=anna.user_id,
            type=PostType.TEXT,
            content="  ",
        )


async def test_public_space_coordinate_truncation(stack):
    """Public space coordinates are truncated to 4dp."""
    _anna = await stack.provision_user("anna")
    s = await stack.space_svc.create_space(
        owner_username="anna",
        name="Pub",
        space_type=SpaceType.PUBLIC,
        join_mode=JoinMode.OPEN,
        lat=52.376543,
        lon=4.895678,
        radius_km=5.0,
    )
    assert s.lat == 52.3765
    assert s.lon == 4.8957


# ── Helper function coverage ──────────────────────────────────────────────


def test_coerce_space_type_string():
    """String space type is coerced to enum."""
    from socialhome.services.space_service import _coerce_space_type
    from socialhome.domain.space import SpaceType

    assert _coerce_space_type("private") is SpaceType.PRIVATE
    assert _coerce_space_type(SpaceType.PUBLIC) is SpaceType.PUBLIC


def test_coerce_space_type_invalid():
    """Invalid space type string raises ValueError."""
    from socialhome.services.space_service import _coerce_space_type

    with pytest.raises(ValueError, match="invalid space type"):
        _coerce_space_type("bogus")


def test_coerce_join_mode_string():
    """String join mode is coerced to enum."""
    from socialhome.services.space_service import _coerce_join_mode
    from socialhome.domain.space import JoinMode

    assert _coerce_join_mode("open") is JoinMode.OPEN
    assert _coerce_join_mode(JoinMode.INVITE_ONLY) is JoinMode.INVITE_ONLY


def test_coerce_join_mode_invalid():
    """Invalid join mode raises ValueError."""
    from socialhome.services.space_service import _coerce_join_mode

    with pytest.raises(ValueError, match="invalid join mode"):
        _coerce_join_mode("bogus")


def test_coerce_post_type():
    """Post type coercion works for strings and enums."""
    from socialhome.services.space_service import _coerce_post_type

    assert _coerce_post_type("text") is PostType.TEXT
    assert _coerce_post_type(PostType.IMAGE) is PostType.IMAGE
    with pytest.raises(ValueError):
        _coerce_post_type("bogus")


def test_coerce_comment_type():
    """Comment type coercion works."""
    from socialhome.services.space_service import _coerce_comment_type
    from socialhome.domain.post import CommentType

    assert _coerce_comment_type("text") is CommentType.TEXT
    assert _coerce_comment_type(CommentType.IMAGE) is CommentType.IMAGE
    with pytest.raises(ValueError):
        _coerce_comment_type("bogus")


def test_validate_space_content_file():
    """File post without file_meta raises ValueError."""
    from socialhome.services.space_service import _validate_space_content

    with pytest.raises(ValueError, match="file_meta"):
        _validate_space_content(PostType.FILE, None, None)


def test_validate_space_content_text_empty():
    """Text post with empty content raises ValueError."""
    from socialhome.services.space_service import _validate_space_content

    with pytest.raises(ValueError, match="content"):
        _validate_space_content(PostType.TEXT, "   ", None)


def test_validate_text_length():
    """Over-length content raises ValueError."""
    from socialhome.services.space_service import _validate_text_length

    with pytest.raises(ValueError, match="maximum length"):
        _validate_text_length("x" * 10001, limit=10000)
    _validate_text_length(None, limit=100)  # None is OK


# ── More service edge paths ───────────────────────────────────────────────


async def test_space_create_unknown_owner(stack):
    """Creating space with unknown owner raises KeyError."""
    with pytest.raises(KeyError, match="owner"):
        await stack.space_svc.create_space(owner_username="ghost", name="X")


async def test_space_create_empty_name(stack):
    """Creating space with empty name raises ValueError."""
    await stack.provision_user("emp")
    with pytest.raises(ValueError, match="empty"):
        await stack.space_svc.create_space(owner_username="emp", name="  ")


async def test_space_update_unknown_actor(stack):
    """update_config with unknown actor raises KeyError."""
    _anna = await stack.provision_user("upd_anna")
    s = await stack.space_svc.create_space(owner_username="upd_anna", name="S")
    with pytest.raises(KeyError):
        await stack.space_svc.update_config(s.id, actor_username="ghost", name="X")


async def test_space_remove_member_unknown_actor(stack):
    """remove_member with unknown actor raises KeyError."""
    _anna = await stack.provision_user("rm_anna")
    s = await stack.space_svc.create_space(owner_username="rm_anna", name="S")
    with pytest.raises(KeyError):
        await stack.space_svc.remove_member(s.id, actor_username="ghost", user_id="x")


async def test_space_edit_post_deleted_rejected(stack):
    """Editing a deleted post raises KeyError."""
    anna = await stack.provision_user("edel_anna")
    s = await stack.space_svc.create_space(owner_username="edel_anna", name="S")
    p = await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="x"
    )
    await stack.space_svc.delete_post(p.id, actor_user_id=anna.user_id)
    with pytest.raises(KeyError, match="deleted"):
        await stack.space_svc.edit_post(
            p.id, editor_user_id=anna.user_id, new_content="y"
        )


async def test_space_comment_image_no_media(stack):
    """Image comment without media_url raises ValueError."""
    anna = await stack.provision_user("img_anna")
    s = await stack.space_svc.create_space(owner_username="img_anna", name="S")
    p = await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="x"
    )
    with pytest.raises(ValueError, match="media_url"):
        await stack.space_svc.add_comment(
            p.id, author_user_id=anna.user_id, comment_type="image"
        )


# ── Subscriptions (read-only membership) ──────────────────────────────────


async def test_subscribe_public_space_adds_subscriber_member(stack):
    """Subscribing to a public space inserts a ``role='subscriber'`` row in
    ``space_members`` — subscribers are read-only members under the hood."""
    owner = await stack.provision_user("owner1")
    fan = await stack.provision_user("fan")
    space = await stack.space_svc.create_space(
        owner_username="owner1", name="P", space_type=SpaceType.GLOBAL
    )
    await stack.space_svc.subscribe_to_space(fan.user_id, space.id)

    assert await stack.space_svc.is_subscribed(fan.user_id, space.id) is True
    member = await stack.space_repo.get_member(space.id, fan.user_id)
    assert member is not None
    assert member.role == "subscriber"
    # The space owner is still an owner, not demoted.
    owner_mem = await stack.space_repo.get_member(space.id, owner.user_id)
    assert owner_mem.role == "owner"


async def test_subscribe_private_space_rejected(stack):
    """Private / household spaces cannot be followed — joining requires
    an invite."""
    await stack.provision_user("owner2")
    fan = await stack.provision_user("fan")
    space = await stack.space_svc.create_space(
        owner_username="owner2", name="Priv", space_type=SpaceType.PRIVATE
    )
    with pytest.raises(SpacePermissionError, match="public / global"):
        await stack.space_svc.subscribe_to_space(fan.user_id, space.id)


async def test_subscribe_is_idempotent(stack):
    """Double-subscribe does not error and does not create duplicate rows."""
    await stack.provision_user("owner3")
    fan = await stack.provision_user("fan")
    space = await stack.space_svc.create_space(
        owner_username="owner3", name="P", space_type=SpaceType.GLOBAL
    )
    await stack.space_svc.subscribe_to_space(fan.user_id, space.id)
    await stack.space_svc.subscribe_to_space(fan.user_id, space.id)
    follows = await stack.space_svc.list_subscriptions(fan.user_id)
    assert len(follows) == 1


async def test_subscribe_does_not_demote_existing_member(stack):
    """An existing real member who calls follow stays at their current
    role — never gets demoted to subscriber."""
    await stack.provision_user("owner4")
    real = await stack.provision_user("real")
    space = await stack.space_svc.create_space(
        owner_username="owner4", name="P", space_type=SpaceType.GLOBAL
    )
    await stack.space_svc.add_member(
        space.id, actor_username="owner4", user_id=real.user_id
    )
    await stack.space_svc.subscribe_to_space(real.user_id, space.id)
    member = await stack.space_repo.get_member(space.id, real.user_id)
    assert member.role == "member"
    # Not listed as a subscriber.
    assert await stack.space_svc.list_subscriptions(real.user_id) == []


async def test_unsubscribe_removes_subscriber_only(stack):
    """Unsubscribe removes a ``role='subscriber'`` row; a real member is
    untouched (so unsubscribe can't be used to silently leave a space)."""
    await stack.provision_user("owner5")
    fan = await stack.provision_user("fan")
    real = await stack.provision_user("real")
    space = await stack.space_svc.create_space(
        owner_username="owner5", name="P", space_type=SpaceType.GLOBAL
    )
    await stack.space_svc.subscribe_to_space(fan.user_id, space.id)
    await stack.space_svc.add_member(
        space.id, actor_username="owner5", user_id=real.user_id
    )

    await stack.space_svc.unsubscribe_from_space(fan.user_id, space.id)
    assert await stack.space_repo.get_member(space.id, fan.user_id) is None

    await stack.space_svc.unsubscribe_from_space(real.user_id, space.id)
    still = await stack.space_repo.get_member(space.id, real.user_id)
    assert still is not None
    assert still.role == "member"


async def test_list_subscriptions_only_returns_subscribers(stack):
    """``list_subscriptions`` filters out spaces where the user is a real
    member — only ``role='subscriber'`` rows are listed."""
    await stack.provision_user("owner6")
    u = await stack.provision_user("multi")
    pub = await stack.space_svc.create_space(
        owner_username="owner6", name="Pub", space_type=SpaceType.GLOBAL
    )
    mem_space = await stack.space_svc.create_space(
        owner_username="owner6", name="Mem", space_type=SpaceType.GLOBAL
    )
    await stack.space_svc.subscribe_to_space(u.user_id, pub.id)
    await stack.space_svc.add_member(
        mem_space.id, actor_username="owner6", user_id=u.user_id
    )
    follows = await stack.space_svc.list_subscriptions(u.user_id)
    assert [r["space_id"] for r in follows] == [pub.id]


async def test_subscriber_cannot_create_post(stack):
    """§ read-only membership: subscribers are rejected on post create."""
    await stack.provision_user("owner7")
    fan = await stack.provision_user("fan")
    space = await stack.space_svc.create_space(
        owner_username="owner7", name="P", space_type=SpaceType.GLOBAL
    )
    await stack.space_svc.subscribe_to_space(fan.user_id, space.id)
    with pytest.raises(SpacePermissionError, match="subscribers can only read"):
        await stack.space_svc.create_post(
            space.id,
            author_user_id=fan.user_id,
            type=PostType.TEXT,
            content="should be blocked",
        )


async def test_subscriber_cannot_comment(stack):
    await stack.provision_user("owner8")
    fan = await stack.provision_user("fan")
    space = await stack.space_svc.create_space(
        owner_username="owner8", name="P", space_type=SpaceType.GLOBAL
    )
    post = await stack.space_svc.create_post(
        space.id,
        author_user_id=(await stack.user_svc.get("owner8")).user_id,
        type=PostType.TEXT,
        content="hi",
    )
    await stack.space_svc.subscribe_to_space(fan.user_id, space.id)
    with pytest.raises(SpacePermissionError, match="subscribers can only read"):
        await stack.space_svc.add_comment(
            post.id, author_user_id=fan.user_id, content="reply"
        )


async def test_subscriber_cannot_react(stack):
    await stack.provision_user("owner9")
    fan = await stack.provision_user("fan")
    space = await stack.space_svc.create_space(
        owner_username="owner9", name="P", space_type=SpaceType.GLOBAL
    )
    post = await stack.space_svc.create_post(
        space.id,
        author_user_id=(await stack.user_svc.get("owner9")).user_id,
        type=PostType.TEXT,
        content="hi",
    )
    await stack.space_svc.subscribe_to_space(fan.user_id, space.id)
    with pytest.raises(SpacePermissionError, match="subscribers can only read"):
        await stack.space_svc.add_reaction(post.id, user_id=fan.user_id, emoji="👍")


async def test_subscribe_banned_user_rejected(stack):
    await stack.provision_user("owner10")
    fan = await stack.provision_user("fan")
    space = await stack.space_svc.create_space(
        owner_username="owner10", name="P", space_type=SpaceType.GLOBAL
    )
    # Seed a ban row directly.
    await stack.space_repo.ban_member(
        space.id, fan.user_id, banned_by="owner10-uid", reason="test"
    )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.subscribe_to_space(fan.user_id, space.id)


async def test_update_config_publishes_location_mode_changed(stack):
    """Flipping ``features.location_mode`` publishes
    :class:`SpaceLocationModeChanged` so SpaceLocationOutbound can
    refire the latest presence under the new tier (§23.8.6)."""
    from socialhome.domain.events import SpaceLocationModeChanged

    captured: list[SpaceLocationModeChanged] = []

    async def _capture(ev: SpaceLocationModeChanged) -> None:
        captured.append(ev)

    stack.space_svc._bus.subscribe(SpaceLocationModeChanged, _capture)

    _a = await stack.provision_user("anna", is_admin=True)
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="Loc",
    )
    # Default mode is gps; flipping to zone_only must publish.
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(location=True, location_mode="zone_only"),
    )
    assert len(captured) == 1
    assert captured[0].space_id == space.id
    assert captured[0].new_mode == "zone_only"

    # Same mode again — no extra publish.
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(location=True, location_mode="zone_only"),
    )
    assert len(captured) == 1

    # Back to gps — publishes again.
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(location=True, location_mode="gps"),
    )
    assert len(captured) == 2
    assert captured[1].new_mode == "gps"


async def test_update_config_publishes_location_feature_enabled_on_off_to_on(stack):
    """Flipping ``feature_location`` from OFF→ON publishes
    :class:`SpaceLocationFeatureEnabled` exactly once."""
    from socialhome.domain.events import SpaceLocationFeatureEnabled

    captured: list[SpaceLocationFeatureEnabled] = []

    async def _capture(ev: SpaceLocationFeatureEnabled) -> None:
        captured.append(ev)

    stack.space_svc._bus.subscribe(SpaceLocationFeatureEnabled, _capture)

    anna = await stack.provision_user("anna", is_admin=True)
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="LocSpace",
    )
    # Default feature_location=False → flip to True.
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(location=True),
    )
    assert len(captured) == 1
    assert captured[0].space_id == space.id
    assert captured[0].space_name == "LocSpace"
    assert captured[0].actor_user_id == anna.user_id


async def test_update_config_does_not_republish_on_idempotent_enable(stack):
    """Flipping ``feature_location`` True→True does NOT publish."""
    from socialhome.domain.events import SpaceLocationFeatureEnabled

    captured: list[SpaceLocationFeatureEnabled] = []

    async def _capture(ev: SpaceLocationFeatureEnabled) -> None:
        captured.append(ev)

    stack.space_svc._bus.subscribe(SpaceLocationFeatureEnabled, _capture)

    _anna = await stack.provision_user("anna", is_admin=True)
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="IdempSpace",
    )
    # First enable — should publish.
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(location=True),
    )
    assert len(captured) == 1
    # Second enable (True→True) — must NOT publish again.
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(location=True),
    )
    assert len(captured) == 1


async def test_update_config_does_not_publish_on_off(stack):
    """Flipping ``feature_location`` True→False does NOT publish."""
    from socialhome.domain.events import SpaceLocationFeatureEnabled

    captured: list[SpaceLocationFeatureEnabled] = []

    async def _capture(ev: SpaceLocationFeatureEnabled) -> None:
        captured.append(ev)

    stack.space_svc._bus.subscribe(SpaceLocationFeatureEnabled, _capture)

    _anna = await stack.provision_user("anna", is_admin=True)
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="OffSpace",
    )
    # Enable then immediately disable — should NOT publish on the disable.
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(location=True),
    )
    assert len(captured) == 1
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(location=False),
    )
    assert len(captured) == 1  # No additional publish


# ─── Forward-secrecy rotation (#121, PR #432) ──────────────────────────


async def test_remove_member_rotates_and_distributes_key(stack):
    """When the host removes a local member, the space epoch MUST
    rotate and the new key MUST federate to every remaining member
    household via SPACE_KEY_EXCHANGE_REKEY. Without rotation, the
    kicked member could keep decrypting future content with their
    cached at-rest key."""
    from unittest.mock import AsyncMock

    from socialhome.domain.federation import FederationEventType

    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(
        space.id, actor_username="anna", user_id=bob.user_id
    )

    space_crypto = AsyncMock()
    space_crypto.rotate_epoch = AsyncMock(return_value=7)
    space_crypto.export_current_key = AsyncMock(return_value=(7, bytes(range(32))))
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    stack.space_svc.attach_space_crypto_service(space_crypto)
    stack.space_svc._federation = federation

    await stack.space_svc.remove_member(
        space.id, actor_username="anna", user_id=bob.user_id
    )

    space_crypto.rotate_epoch.assert_awaited_once_with(space.id)
    # Removal now also emits a SPACE_MEMBER_LEFT roster gossip (v_23), so the
    # rekey is one of the broadcasts — assert it fired with the right payload.
    rekey_calls = [
        c
        for c in federation.broadcast_to_space_members.await_args_list
        if c.args[1] is FederationEventType.SPACE_KEY_EXCHANGE_REKEY
    ]
    assert len(rekey_calls) == 1
    payload = rekey_calls[0].args[2]
    assert payload["space_id"] == space.id
    meta = payload["space_content_key"]
    assert meta["epoch"] == 7
    assert meta["key_suite"] == "aesgcm-256"
    # SECURITY (rekey authority gate): the owner host signs the rekey meta with
    # the space seed so receivers authenticate the rotator before importing.
    from socialhome.services.space_crypto_service import (
        strip_authority_sig_fields,
        verify_authority_event,
    )

    stored = await stack.space_repo.get(space.id)
    assert verify_authority_event(
        event_type="space_key_exchange_rekey",
        space_id=space.id,
        payload=strip_authority_sig_fields(meta),
        authority_sig=meta["authority_sig"],
        authority_sig_suite=meta["authority_sig_suite"],
        space_public_key=bytes.fromhex(stored.identity_public_key),
    )


async def test_ban_rotates_and_distributes_key(stack):
    """Ban is also a kick — same forward-secrecy guarantee applies."""
    from unittest.mock import AsyncMock

    from socialhome.domain.federation import FederationEventType

    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(
        space.id, actor_username="anna", user_id=bob.user_id
    )

    space_crypto = AsyncMock()
    space_crypto.rotate_epoch = AsyncMock(return_value=11)
    space_crypto.export_current_key = AsyncMock(return_value=(11, bytes(range(32))))
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    stack.space_svc.attach_space_crypto_service(space_crypto)
    stack.space_svc._federation = federation

    await stack.space_svc.ban(
        space.id, actor_username="anna", user_id=bob.user_id, reason="spam"
    )

    space_crypto.rotate_epoch.assert_awaited_once_with(space.id)
    # Ban now also emits a SPACE_MEMBER_LEFT roster gossip (v_23); assert the
    # rekey is among the broadcasts.
    assert any(
        c.args[1] is FederationEventType.SPACE_KEY_EXCHANGE_REKEY
        for c in federation.broadcast_to_space_members.await_args_list
    )


async def test_remove_member_without_crypto_attached_is_noop(stack):
    """Without ``SpaceContentEncryption`` wired (early boot / unit
    test stacks), removal still succeeds — the rotation helper just
    no-ops rather than crashing the kick."""
    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(
        space.id, actor_username="anna", user_id=bob.user_id
    )
    # Default stack has neither crypto nor federation attached.
    assert stack.space_svc._space_crypto is None
    assert stack.space_svc._federation is None
    await stack.space_svc.remove_member(
        space.id, actor_username="anna", user_id=bob.user_id
    )
    members = await stack.space_repo.list_members(space.id)
    assert len(members) == 1


async def test_rotation_broadcast_failure_does_not_break_kick(stack):
    """If the federation broadcast fails mid-rotation, the kick MUST
    still succeed (the local member is gone). The next kick / ban
    retries rotation; sync handshake catches up missed peers."""
    from unittest.mock import AsyncMock

    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(
        space.id, actor_username="anna", user_id=bob.user_id
    )

    space_crypto = AsyncMock()
    space_crypto.rotate_epoch = AsyncMock(return_value=3)
    space_crypto.export_current_key = AsyncMock(return_value=(3, bytes(range(32))))
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock(
        side_effect=RuntimeError("transport down")
    )
    stack.space_svc.attach_space_crypto_service(space_crypto)
    stack.space_svc._federation = federation

    # Should not raise.
    await stack.space_svc.remove_member(
        space.id, actor_username="anna", user_id=bob.user_id
    )
    members = await stack.space_repo.list_members(space.id)
    assert len(members) == 1


async def test_dissolve_hard_deletes_content_and_unlinks_media(tmp_dir):
    """Dissolving a space drops its content (FK cascade) and unlinks every
    media file it owned — posts + multi-image + gallery."""
    import pathlib
    from unittest.mock import AsyncMock, MagicMock

    from socialhome.domain.federation import FederationEventType
    from socialhome.repositories.bazaar_repo import SqliteBazaarRepo
    from socialhome.repositories.gallery_repo import SqliteGalleryRepo

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "hd.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        """INSERT INTO instance_identity(instance_id, identity_private_key,
           identity_public_key, routing_secret) VALUES(?,?,?,?)""",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    from socialhome.infrastructure.key_manager import KeyManager

    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    space_repo = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0b" * 32))
    post_repo = SqliteSpacePostRepo(db)
    gallery = SqliteGalleryRepo(db)
    bazaar = SqliteBazaarRepo(db)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    media = pathlib.Path(tmp_dir) / "media"
    media.mkdir()
    for name in (
        "pic.webp",
        "img2.webp",
        "gal.webp",
        "galthumb.webp",
        "baz.webp",
        "keep.webp",
    ):
        (media / name).write_bytes(b"X")
    svc = SpaceService(
        space_repo, post_repo, user_repo, bus, own_instance_id=iid, media_dir=media
    )
    svc.attach_gallery_repo(gallery)
    svc.attach_bazaar_repo(bazaar)
    # Spy federation so we can assert SPACE_DISSOLVED is broadcast to members.
    fed = MagicMock()
    fed.broadcast_to_space_members = AsyncMock()
    svc._federation = fed

    anna = await user_svc.provision(username="anna", display_name="A", is_admin=True)
    space = await svc.create_space(owner_username="anna", name="Fam")
    await db.enqueue(
        """INSERT INTO space_posts(id, space_id, author, type, media_url,
           image_urls_json) VALUES(?,?,?,?,?,?)""",
        (
            "p1",
            space.id,
            anna.user_id,
            "image",
            "api/media/pic.webp",
            '["api/media/img2.webp"]',
        ),
    )
    await db.enqueue(
        "INSERT INTO gallery_albums(id, space_id, name) VALUES(?,?,?)",
        ("al1", space.id, "Album"),
    )
    await db.enqueue(
        """INSERT INTO gallery_items(id, album_id, uploaded_by, item_type,
           filename, thumbnail_filename, width, height)
           VALUES(?,?,?,?,?,?,?,?)""",
        ("gi1", "al1", anna.user_id, "photo", "gal.webp", "galthumb.webp", 1, 1),
    )
    # Bazaar listing: the wrapper post carries no image, so the photo
    # lives only on the listing row and would leak without bazaar
    # collection.
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type) VALUES(?,?,?,?)",
        ("pb", space.id, anna.user_id, "bazaar"),
    )
    await db.enqueue(
        """INSERT INTO bazaar_listings(post_id, space_id, seller_user_id, mode,
           title, image_urls_json, end_time, currency)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            "pb",
            space.id,
            anna.user_id,
            "fixed",
            "Chair",
            '["api/media/baz.webp"]',
            "2030-01-01T00:00:00+00:00",
            "EUR",
        ),
    )

    # Sanity: media is collectable + files present pre-dissolve.
    assert set(await post_repo.list_space_media_urls(space.id)) == {
        "api/media/pic.webp",
        "api/media/img2.webp",
    }
    assert set(await gallery.list_space_item_filenames(space.id)) == {
        "gal.webp",
        "galthumb.webp",
    }

    assert await bazaar.list_space_media_urls(space.id) == ["api/media/baz.webp"]

    await svc.dissolve_space(space.id, actor_username="anna")

    # Members are told to hard-delete their copy too.
    fed.broadcast_to_space_members.assert_awaited_once()
    bcall = fed.broadcast_to_space_members.await_args
    assert bcall.args[0] == space.id
    assert bcall.args[1] == FederationEventType.SPACE_DISSOLVED
    assert bcall.args[2] == {"space_id": space.id}

    # Space + all content rows gone (cascade).
    with pytest.raises(KeyError):
        await svc.list_feed(space.id)
    assert await post_repo.list_space_media_urls(space.id) == []
    assert await gallery.list_space_item_filenames(space.id) == []
    assert await bazaar.list_space_media_urls(space.id) == []
    assert await space_repo.get(space.id) is None

    # Every owned media file unlinked; the unrelated file survives.
    for gone in ("pic.webp", "img2.webp", "gal.webp", "galthumb.webp", "baz.webp"):
        assert not (media / gone).exists(), gone
    assert (media / "keep.webp").exists()
    await db.shutdown()


async def test_archive_makes_space_read_only_and_reversible(stack):
    """Archive = soft + reversible: rows stay, the space is still readable,
    but content writes are rejected until unarchived."""
    a = await stack.provision_user("anna", is_admin=True)
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    p = await stack.space_svc.create_post(
        space.id, author_user_id=a.user_id, type=PostType.TEXT, content="hi"
    )
    assert p is not None

    await stack.space_svc.archive_space(space.id, actor_username="anna")
    refreshed = await stack.space_repo.get(space.id)
    assert refreshed.archived is True
    # Still readable (rows retained, not hard-deleted).
    await stack.space_svc.list_feed(space.id)
    # Writes rejected.
    with pytest.raises(SpacePermissionError, match="archived"):
        await stack.space_svc.create_post(
            space.id, author_user_id=a.user_id, type=PostType.TEXT, content="no"
        )
    with pytest.raises(SpacePermissionError, match="archived"):
        await stack.space_svc.add_comment(p.id, author_user_id=a.user_id, content="no")
    with pytest.raises(SpacePermissionError, match="archived"):
        await stack.space_svc.add_reaction(p.id, user_id=a.user_id, emoji="👍")

    # Reversible: unarchive restores read-write.
    await stack.space_svc.unarchive_space(space.id, actor_username="anna")
    assert (await stack.space_repo.get(space.id)).archived is False
    p2 = await stack.space_svc.create_post(
        space.id, author_user_id=a.user_id, type=PostType.TEXT, content="again"
    )
    assert p2 is not None


async def test_archive_federates_via_space_meta(stack):
    """The archived flag rides the federation metadata snapshot so member
    households apply it through the normal config-change stub refresh."""
    from socialhome.services.space_service import (
        _space_metadata_for_federation,
        stub_space_from_metadata,
    )

    await stack.provision_user("anna", is_admin=True)
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.archive_space(space.id, actor_username="anna")
    refreshed = await stack.space_repo.get(space.id)

    meta = _space_metadata_for_federation(refreshed)
    assert meta["archived"] is True
    stub = stub_space_from_metadata(
        space.id, host_instance_id=refreshed.owner_instance_id, meta=meta
    )
    assert stub.archived is True


async def test_delegated_admin_authority_federates_via_space_meta(stack):
    """The owner's delegated_admin_authority opt-in rides the federation
    metadata snapshot AND a joiner's stub carries it locally.

    Regression: a multi-node demo found the flag never crossed the wire —
    _space_metadata_for_federation dropped it and stub_space_from_metadata
    defaulted it OFF, so a §D1b joiner / config-flip receiver never enabled
    delegation locally and rejected the space signing seed.
    """
    from socialhome.services.space_service import (
        _space_metadata_for_federation,
        stub_space_from_metadata,
    )

    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="Deleg")
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(delegated_admin_authority=True),
    )
    refreshed = await stack.space_repo.get(space.id)
    assert refreshed.features.delegated_admin_authority is True

    # Fix A.2 — the metadata snapshot carries the flag.
    meta = _space_metadata_for_federation(refreshed)
    assert meta["features"]["delegated_admin_authority"] is True

    # Fix A.3 — the receiver-side stub reads it back as True.
    stub = stub_space_from_metadata(
        space.id, host_instance_id=refreshed.owner_instance_id, meta=meta
    )
    assert stub.features.delegated_admin_authority is True


async def test_delegated_admin_authority_missing_from_meta_defaults_false(stack):
    """Fail-soft: an older sender omits delegated_admin_authority → the stub
    defaults it OFF (the strict, owner-must-opt-in contract)."""
    from socialhome.services.space_service import stub_space_from_metadata

    stub = stub_space_from_metadata(
        "sp-legacy",
        host_instance_id="h",
        meta={"name": "Legacy", "features": {}},
    )
    assert stub.features.delegated_admin_authority is False


async def test_min_age_federates_via_space_meta_and_persists(stack):
    """§CP.F1 — the host's age gate rides the federation metadata, the stub
    carries it, and a save()/get() round-trip persists it (so a member
    household's join paths can enforce the host's gate locally)."""
    from socialhome.services.space_service import (
        _space_metadata_for_federation,
        stub_space_from_metadata,
    )

    await stack.provision_user("anna", is_admin=True)
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_repo.update_age_gate(
        space.id,
        min_age=18,
        target_audience="adult",
    )
    refreshed = await stack.space_repo.get(space.id)
    assert refreshed.min_age == 18  # _row_to_space reads the column

    meta = _space_metadata_for_federation(refreshed)
    assert meta["min_age"] == 18
    assert meta["target_audience"] == "adult"

    stub = stub_space_from_metadata(
        "remote-sp",
        host_instance_id="remote-host",
        meta=meta,
    )
    assert stub.min_age == 18
    assert stub.target_audience == "adult"
    # Persist the stub and confirm save()/get() round-trips min_age (the
    # gate reads it from the DB, so it must survive the upsert).
    await stack.space_repo.save(stub)
    seated = await stack.space_repo.get("remote-sp")
    assert seated.min_age == 18
    assert seated.target_audience == "adult"


async def test_min_age_missing_from_meta_defaults_to_zero(stack):
    """Fail-soft: an older sender omits min_age → stub defaults to 0 (no
    restriction), matching the pre-federation behaviour."""
    from socialhome.services.space_service import stub_space_from_metadata

    stub = stub_space_from_metadata(
        "sp-x",
        host_instance_id="h",
        meta={"name": "Legacy"},
    )
    assert stub.min_age == 0
    assert stub.target_audience == "all"


async def test_icon_hash_federates_via_space_meta(stack):
    """icon_hash rides the federation metadata + the stub carries it, so a
    member household renders the host's icon (after the bytes arrive via
    icon_webp_base64)."""
    from socialhome.services.space_service import (
        _space_metadata_for_federation,
        stub_space_from_metadata,
    )

    await stack.provision_user("anna", is_admin=True)
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_repo.set_icon_hash(space.id, "feedface")
    refreshed = await stack.space_repo.get(space.id)
    meta = _space_metadata_for_federation(refreshed)
    assert meta["icon_hash"] == "feedface"
    stub = stub_space_from_metadata(
        space.id, host_instance_id=refreshed.owner_instance_id, meta=meta
    )
    assert stub.icon_hash == "feedface"


async def test_apply_space_icon_from_metadata_persists_bytes(stack):
    """The joiner-side helper decodes + persists the host's icon bytes."""
    import base64

    from socialhome.services.space_service import apply_space_icon_from_metadata

    class _IconRepo:
        def __init__(self):
            self.saved = None

        async def set(self, space_id, *, bytes_webp, hash, width, height):
            self.saved = (space_id, bytes_webp, hash)

    repo = _IconRepo()
    raw = b"RIFFwebp-icon"
    await apply_space_icon_from_metadata(
        "sp-x",
        meta={
            "icon_hash": "abc123",
            "icon_webp_base64": base64.b64encode(raw).decode("ascii"),
        },
        icon_repo=repo,
    )
    assert repo.saved == ("sp-x", raw, "abc123")
    # No bytes → no write.
    repo2 = _IconRepo()
    await apply_space_icon_from_metadata(
        "sp-x", meta={"icon_hash": "h"}, icon_repo=repo2
    )
    assert repo2.saved is None


async def test_allowed_post_types_federate_via_space_meta(stack):
    """The per-space post-type allow-list rides the federation metadata so a
    member household enforces the same restriction when its users compose."""
    from socialhome.services.space_service import (
        _space_metadata_for_federation,
        stub_space_from_metadata,
    )

    await stack.provision_user("anna", is_admin=True)
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    restricted = space.features.with_allowed_post_types({"text", "image"})
    await stack.space_svc.update_config(
        space.id, actor_username="anna", features=restricted
    )
    refreshed = await stack.space_repo.get(space.id)
    assert set(refreshed.features.allowed_post_types) == {"text", "image"}

    meta = _space_metadata_for_federation(refreshed)
    assert sorted(meta["features"]["allowed_post_types"]) == ["image", "text"]

    stub = stub_space_from_metadata(
        space.id, host_instance_id=refreshed.owner_instance_id, meta=meta
    )
    assert set(stub.features.allowed_post_types) == {"text", "image"}


def test_stub_space_defaults_all_post_types_when_meta_omits_them():
    """An older sender omits ``allowed_post_types`` → the receiver defaults
    to all types allowed (the pre-federation behaviour), never an accidental
    text-only lockdown."""
    from socialhome.domain.space import _ALL_POST_TYPES
    from socialhome.services.space_service import stub_space_from_metadata

    stub = stub_space_from_metadata(
        "sp-x",
        host_instance_id="inst-h",
        meta={"name": "X", "features": {"pages": True}},
    )
    assert stub.features.allowed_post_types == _ALL_POST_TYPES


# ─── §CP.F1: age gate on EVERY seating path ──────────────────────────────
#
# Regression for the bypass found in the parent+children walkthrough: the
# gate was enforced on add_member/subscribe but NOT on the invite-acceptance
# and join-request-approval seating paths, so a protected minor could still
# land in an 18+ space via a link, an invite, or an approved request.


async def _attach_cp(stack):
    cp = ChildProtectionService(
        SqliteCpRepo(stack.db),
        SqliteUserRepo(stack.db),
        EventBus(),
    )
    stack.space_svc.attach_child_protection(cp)
    return cp


async def test_age_gate_blocks_minor_on_approve_join_request(stack):
    anna = await stack.provision_user("anna", is_admin=True)
    kid = await stack.provision_user("kid")
    cp = await _attach_cp(stack)
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="Adults",
        join_mode=JoinMode.OPEN,
    )
    await cp.enable_protection(
        minor_username="kid",
        declared_age=8,
        actor_user_id=anna.user_id,
    )
    await cp.update_space_age_gate(
        space.id,
        min_age=18,
        target_audience="adult",
        actor_user_id=anna.user_id,
    )
    req_id = await stack.space_svc.request_join(space.id, user_id=kid.user_id)
    with pytest.raises(SpacePermissionError, match="18"):
        await stack.space_svc.approve_join_request(req_id, actor_username="anna")
    assert await stack.space_repo.get_member(space.id, kid.user_id) is None


async def test_age_gate_blocks_minor_on_accept_invite_token(stack):
    anna = await stack.provision_user("anna", is_admin=True)
    kid = await stack.provision_user("kid")
    cp = await _attach_cp(stack)
    space = await stack.space_svc.create_space(owner_username="anna", name="Adults")
    await cp.enable_protection(
        minor_username="kid",
        declared_age=8,
        actor_user_id=anna.user_id,
    )
    await cp.update_space_age_gate(
        space.id,
        min_age=18,
        target_audience="adult",
        actor_user_id=anna.user_id,
    )
    tok = await stack.space_svc.create_invite_token(
        space.id,
        actor_username="anna",
        uses=1,
    )
    with pytest.raises(SpacePermissionError, match="18"):
        await stack.space_svc.accept_invite_token(tok, user_id=kid.user_id)
    assert await stack.space_repo.get_member(space.id, kid.user_id) is None


async def test_age_gate_blocks_minor_on_accept_local_invite(stack):
    """Protection enabled AFTER the invite was sent must still block at
    acceptance (the invite-creation gate can't see a not-yet-minor)."""
    anna = await stack.provision_user("anna", is_admin=True)
    kid = await stack.provision_user("kid")
    cp = await _attach_cp(stack)
    space = await stack.space_svc.create_space(owner_username="anna", name="Adults")
    await cp.update_space_age_gate(
        space.id,
        min_age=18,
        target_audience="adult",
        actor_user_id=anna.user_id,
    )
    # Invite while 'kid' is NOT yet protected, so invite_local_user's own
    # gate doesn't fire — the acceptance gate is what must catch it.
    invitation_id = await stack.space_svc.invite_local_user(
        space.id,
        actor_username="anna",
        user_id=kid.user_id,
    )
    await cp.enable_protection(
        minor_username="kid",
        declared_age=8,
        actor_user_id=anna.user_id,
    )
    with pytest.raises(SpacePermissionError, match="18"):
        await stack.space_svc.accept_local_invite(invitation_id, user_id=kid.user_id)
    assert await stack.space_repo.get_member(space.id, kid.user_id) is None


async def test_age_gate_allows_older_minor_through_seating_paths(stack):
    """A 16-year-old minor is allowed into a 13+ space via approve — the
    gate blocks only when declared_age < min_age, on every path."""
    anna = await stack.provision_user("anna", is_admin=True)
    teen = await stack.provision_user("teen")
    cp = await _attach_cp(stack)
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="Teens",
        join_mode=JoinMode.OPEN,
    )
    await cp.enable_protection(
        minor_username="teen",
        declared_age=16,
        actor_user_id=anna.user_id,
    )
    await cp.update_space_age_gate(
        space.id,
        min_age=13,
        target_audience="teen",
        actor_user_id=anna.user_id,
    )
    req_id = await stack.space_svc.request_join(space.id, user_id=teen.user_id)
    member = await stack.space_svc.approve_join_request(req_id, actor_username="anna")
    assert member is not None and member.user_id == teen.user_id


async def test_can_seat_remote_stub_owner_guard(stack):
    """§D1b anti-hijack helper: a new space is seatable; re-seating by the
    same owner is fine; a different owner is refused (compared against the
    authenticated issuer, never the meta-claimed owner)."""
    from socialhome.services.space_service import (
        can_seat_remote_stub,
        stub_space_from_metadata,
    )

    await stack.provision_user("anna", is_admin=True)
    # No local row → seatable by anyone.
    assert await can_seat_remote_stub(stack.space_repo, "ghost", "host-a") is True
    # Seed a stub owned by host-a.
    await stack.space_repo.save(
        stub_space_from_metadata(
            "shared",
            host_instance_id="host-a",
            meta={"name": "S", "owner_instance_id": "host-a"},
        ),
    )
    # Same owner re-seats; a different host is refused.
    assert await can_seat_remote_stub(stack.space_repo, "shared", "host-a") is True
    assert await can_seat_remote_stub(stack.space_repo, "shared", "host-b") is False


async def test_stub_space_uses_authenticated_sender_as_owner():
    """§D1b — stub_space_from_metadata stamps the AUTHENTICATED sender
    (host_instance_id) as owner, ignoring a spoofed meta['owner_instance_id']
    so a malicious issuer can't forge the owner on a brand-new stub (which
    can_seat_remote_stub would then trust on later events)."""
    from socialhome.services.space_service import stub_space_from_metadata

    stub = stub_space_from_metadata(
        "sp-x",
        host_instance_id="real-sender",
        meta={"name": "S", "owner_instance_id": "spoofed-host"},
    )
    assert stub.owner_instance_id == "real-sender"


# ─── space_version_compat (#319 ¶5) ───────────────────────────────────────


def _member(instance_id, proto_version, *, seen, name=None):
    """Craft a RemoteInstance-like member household row."""
    from socialhome.domain.federation import RemoteInstance

    return RemoteInstance(
        id=instance_id,
        display_name=name or instance_id,
        remote_identity_pk="ab" * 32,
        key_self_to_remote="x",
        key_remote_to_self="y",
        remote_inbox_url="https://peer/inbox",
        local_inbox_id="inbox",
        proto_version=proto_version,
        capabilities_seen_at="2026-06-04T00:00:00+00:00" if seen else None,
    )


class _FakeFedRepo:
    def __init__(self, members):
        self._members = members

    async def list_instances_in_space(self, space_id):
        return list(self._members)


async def test_space_version_compat_flags_behind_member(stack):
    """A known member at v13 surfaces in behind_members with its lacking
    space features; min + lagging reflect the weakest known member."""
    from socialhome.domain.federation_capabilities import OURS

    await stack.provision_user("anna", is_admin=True)
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    stack.space_svc._federation_repo = _FakeFedRepo(
        [_member("peer-13", 13, seen=True, name="Brother's house")]
    )

    c = await stack.space_svc.space_version_compat(space.id, actor_username="anna")
    assert c.ours == OURS
    assert c.min_member_proto_version == 13
    assert c.lagging_features == (
        "Media DataChannel",
        "Remote admin actions",
        "Multi-admin approvals",
        "Authenticated mesh route discovery",
        "Space delegated admin authority",
        "Space roster gossip",
        "Admin authoritative config offline",
    )
    assert len(c.behind_members) == 1
    bm = c.behind_members[0]
    assert bm.instance_id == "peer-13"
    assert bm.display_name == "Brother's house"
    assert bm.proto_version == 13
    assert bm.lacking_features == (
        "Media DataChannel",
        "Remote admin actions",
        "Multi-admin approvals",
        "Authenticated mesh route discovery",
        "Space delegated admin authority",
        "Space roster gossip",
        "Admin authoritative config offline",
    )


async def test_space_version_compat_excludes_mid_handshake_member(stack):
    """A member that has never advertised capabilities (seen_at=None) is
    excluded entirely — not counted in min, not in behind_members."""
    await stack.provision_user("anna", is_admin=True)
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    stack.space_svc._federation_repo = _FakeFedRepo(
        [
            _member("peer-up", 18, seen=True),
            _member("peer-mystery", 1, seen=False),
        ]
    )

    c = await stack.space_svc.space_version_compat(space.id, actor_username="anna")
    # The seen v18 member is the only counted one — phantom-nag guard
    # (peer-mystery, never-advertised, is excluded entirely). The v18
    # member legitimately lags the v_21 authenticated-route-discovery
    # space feature, so it surfaces in behind_members.
    assert c.min_member_proto_version == 18
    assert c.lagging_features == (
        "Authenticated mesh route discovery",
        "Space delegated admin authority",
        "Space roster gossip",
        "Admin authoritative config offline",
    )
    assert len(c.behind_members) == 1
    assert c.behind_members[0].instance_id == "peer-up"


async def test_space_version_compat_all_current(stack):
    """A member at OURS leaves behind_members + lagging empty."""
    from socialhome.domain.federation_capabilities import OURS

    await stack.provision_user("anna", is_admin=True)
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    stack.space_svc._federation_repo = _FakeFedRepo(
        [_member("peer-ours", OURS, seen=True)]
    )

    c = await stack.space_svc.space_version_compat(space.id, actor_username="anna")
    assert c.min_member_proto_version == OURS
    assert c.lagging_features == ()
    assert c.behind_members == ()


async def test_space_version_compat_omits_nonspace_features(stack):
    """A member at v16 is < OURS; its missing features include non-space
    surfaces (app channels v17/v18, instance resync v19, space-sync-reject
    v20 — deliberately NOT space-scoped) plus the one space surface above
    it (authenticated route discovery v21). Only the SPACE-scoped gap
    appears in lagging_features — the non-space ones are never added even
    though the member lacks them."""
    await stack.provision_user("anna", is_admin=True)
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    stack.space_svc._federation_repo = _FakeFedRepo([_member("peer-16", 16, seen=True)])

    c = await stack.space_svc.space_version_compat(space.id, actor_username="anna")
    assert c.min_member_proto_version == 16
    # Only the space-scoped gaps surface; non-space gaps (v17/v18/v19/v20)
    # do not.
    assert c.lagging_features == (
        "Authenticated mesh route discovery",
        "Space delegated admin authority",
        "Space roster gossip",
        "Admin authoritative config offline",
    )
    assert "App federation channel" not in c.lagging_features
    assert "App user routing" not in c.lagging_features
    assert "Instance resync request" not in c.lagging_features
    assert "Space sync reject reconcile" not in c.lagging_features
    assert len(c.behind_members) == 1
    assert c.behind_members[0].instance_id == "peer-16"


async def test_space_version_compat_no_federation_repo(stack):
    """No federation repo wired → empty compat at OURS."""
    from socialhome.domain.federation_capabilities import OURS

    await stack.provision_user("anna", is_admin=True)
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    stack.space_svc._federation_repo = None

    c = await stack.space_svc.space_version_compat(space.id, actor_username="anna")
    assert c.ours == OURS
    assert c.min_member_proto_version is None
    assert c.lagging_features == ()
    assert c.behind_members == ()


async def test_space_version_compat_requires_admin(stack):
    """A non-admin member is refused."""
    await stack.provision_user("anna", is_admin=True)
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(
        space.id, actor_username="anna", user_id=bob.user_id
    )
    stack.space_svc._federation_repo = _FakeFedRepo([])

    with pytest.raises(SpacePermissionError):
        await stack.space_svc.space_version_compat(space.id, actor_username="bob")


async def test_unarchive_normally_archived_space_succeeds(stack):
    """An admin-archived space (``archived_reason=None``) unarchives fine."""
    await stack.provision_user("anna", is_admin=True)
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.archive_space(space.id, actor_username="anna")
    assert (await stack.space_repo.get(space.id)).archived is True

    await stack.space_svc.unarchive_space(space.id, actor_username="anna")
    refreshed = await stack.space_repo.get(space.id)
    assert refreshed.archived is False
    assert refreshed.archived_reason is None


async def test_unarchive_remote_terminated_space_is_rejected(stack):
    """A space that ended on its host (``archived_reason='dissolved'``) must
    not be unarchivable — it can't be revived from the member's side."""
    await stack.provision_user("anna", is_admin=True)
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_repo.set_archived(space.id, True, reason="dissolved")

    with pytest.raises(SpacePermissionError, match="ended on its host"):
        await stack.space_svc.unarchive_space(space.id, actor_username="anna")
    # Still archived — the guard refused before applying.
    assert (await stack.space_repo.get(space.id)).archived is True


# ── Space authority key (Ed25519 seed persistence, phase 0) ──────────────────


async def test_create_space_stores_seed_matching_public_key(stack):
    """create_space persists a private seed whose signature verifies against
    the published identity_public_key (the stored private matches the public)."""
    from socialhome.crypto import sign_ed25519, verify_ed25519

    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="Fam")

    seed = await stack.space_repo.get_space_seed(space.id)
    assert seed is not None
    assert len(seed) == 32

    msg = b"space-authority-test"
    sig = sign_ed25519(seed, msg)
    pub = bytes.fromhex(space.identity_public_key)
    assert verify_ed25519(pub, msg, sig)


async def test_ensure_space_seed_returns_existing(stack):
    """ensure_space_seed returns the already-stored seed for an owned space
    without minting a new one (the pubkey is unchanged)."""
    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="Fam")
    stored = await stack.space_repo.get_space_seed(space.id)

    got = await stack.space_svc.ensure_space_seed(space.id)
    assert got == stored
    # identity_public_key untouched.
    assert (await stack.space_repo.get(space.id)).identity_public_key == (
        space.identity_public_key
    )


async def test_ensure_space_seed_mints_for_owned_null_seed(stack):
    """A pre-upgrade owned space (seed column NULL) gets a fresh keypair minted;
    the new pubkey replaces identity_public_key and verifies against the seed."""
    from socialhome.crypto import sign_ed25519, verify_ed25519

    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="Fam")
    old_pub = space.identity_public_key
    # Simulate a pre-upgrade row: the private key was discarded.
    await stack.db.enqueue(
        "UPDATE spaces SET identity_private_key=NULL WHERE id=?", (space.id,)
    )
    assert await stack.space_repo.get_space_seed(space.id) is None

    seed = await stack.space_svc.ensure_space_seed(space.id)
    assert seed is not None and len(seed) == 32

    refreshed = await stack.space_repo.get(space.id)
    assert refreshed.identity_public_key != old_pub
    sig = sign_ed25519(seed, b"x")
    assert verify_ed25519(bytes.fromhex(refreshed.identity_public_key), b"x", sig)
    # And it's now durably stored.
    assert await stack.space_repo.get_space_seed(space.id) == seed


async def test_ensure_space_seed_never_mints_for_non_owned_space(stack):
    """A space owned by another household with a NULL seed → never mint;
    returns None (or raises) and leaves identity_public_key untouched."""
    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="Fam")
    old_pub = space.identity_public_key
    # Make it a remote-owned space with no stored seed.
    await stack.db.enqueue(
        "UPDATE spaces SET owner_instance_id='other-household',"
        " identity_private_key=NULL WHERE id=?",
        (space.id,),
    )

    result = await stack.space_svc.ensure_space_seed(space.id)
    assert result is None
    # Untouched — no fresh identity minted for a space we don't own.
    assert (await stack.space_repo.get(space.id)).identity_public_key == old_pub
    assert await stack.space_repo.get_space_seed(space.id) is None


async def test_seed_never_appears_in_federation_snapshot(stack):
    """The private seed must never leak into the federation snapshot."""
    from socialhome.services.space_service import (
        build_space_snapshot_for_federation,
    )

    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="Fam")
    seed = await stack.space_repo.get_space_seed(space.id)
    assert seed is not None

    snap = await build_space_snapshot_for_federation(
        space,
        space_repo=stack.space_repo,
        remote_member_repo=None,
        user_repo=stack.space_svc._users,
        own_instance_id=stack.iid,
    )
    blob = repr(snap)
    assert seed.hex() not in blob
    assert "identity_private_key" not in blob
    assert "private_key" not in blob


async def test_snapshot_carries_member_versions_and_roster_version(stack):
    """The §D1b snapshot ships a roster_version + a member_version per roster
    entry (v_23) so a freshly-invited joiner starts already-converged."""
    from socialhome.repositories.space_remote_member_repo import (
        SqliteSpaceRemoteMemberRepo,
    )
    from socialhome.services.space_service import (
        build_space_snapshot_for_federation,
    )

    anna = await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="Fam")
    remote = SqliteSpaceRemoteMemberRepo(stack.db)
    # Seat a remote member with a known member_version via the merge path.
    await remote.apply_member_event(
        space_id=space.id,
        user_id="ru1",
        instance_id="peer-x",
        display_name="R",
        user_pk=None,
        role="member",
        member_version=5,
        tombstoned=False,
    )

    snap = await build_space_snapshot_for_federation(
        space,
        space_repo=stack.space_repo,
        remote_member_repo=remote,
        user_repo=stack.space_svc._users,
        own_instance_id=stack.iid,
    )

    assert "roster_version" in snap
    assert isinstance(snap["roster_version"], int)
    roster = {r["user_id"]: r for r in snap["roster"]}
    # Local owner entry carries a member_version (0 default for a row never
    # gossiped) and the remote entry carries its merged version.
    assert "member_version" in roster[anna.user_id]
    assert roster["ru1"]["member_version"] == 5


# ─── Delegated-admin signing-seed share — outbound (v_22) ──────────────


def _seed_share_fed(*, supports=True):
    """An AsyncMock federation service whose peer_supports + send paths
    are wired for the SPACE_ADMIN_KEY_SHARE outbound assertions."""
    from unittest.mock import AsyncMock

    from socialhome.domain.federation import DeliveryResult

    fed = AsyncMock()
    fed.peer_supports = AsyncMock(return_value=supports)
    fed.send_with_mesh_fallback = AsyncMock(
        return_value=DeliveryResult(instance_id="x", ok=True)
    )
    fed.broadcast_to_space_members = AsyncMock()
    return fed


async def _wire_remote_members(stack):
    """Attach a real SqliteSpaceRemoteMemberRepo so list_admin_instances
    + role writes hit the same DB the service reads."""
    from socialhome.repositories.space_remote_member_repo import (
        SqliteSpaceRemoteMemberRepo,
    )

    repo = SqliteSpaceRemoteMemberRepo(stack.db)
    stack.space_svc._remote_members = repo
    return repo


async def test_promote_remote_admin_shares_seed_when_delegation_on(stack):
    """Promoting a remote member to ADMIN in a delegation-ON owned space
    ships SPACE_ADMIN_KEY_SHARE to that admin's household with the space's
    32-byte signing seed (b64url) + the ed25519-seed suite tag."""
    import base64

    from socialhome.domain.federation import FederationEventType
    from socialhome.domain.space import SpaceFeatures, SpaceRole

    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="S",
        features=SpaceFeatures(delegated_admin_authority=True),
    )
    fed = _seed_share_fed()
    stack.space_svc._federation = fed
    remote = await _wire_remote_members(stack)
    await remote.add(
        space_id=space.id,
        instance_id="peer-x",
        user_id="ru1",
        user_pk=None,
        display_name=None,
    )

    await stack.space_svc.set_remote_member_role(
        space.id,
        actor_username="anna",
        instance_id="peer-x",
        user_id="ru1",
        role=SpaceRole.ADMIN,
    )

    # The role-changed broadcast still fires.
    fed.broadcast_to_space_members.assert_awaited()
    # The seed share went via the encrypted peer-pair path, to peer-x only.
    fed.send_with_mesh_fallback.assert_awaited_once()
    kw = fed.send_with_mesh_fallback.await_args.kwargs
    assert kw["to_instance_id"] == "peer-x"
    assert kw["event_type"] is FederationEventType.SPACE_ADMIN_KEY_SHARE
    p = kw["payload"]
    assert p["space_id"] == space.id
    assert p["seed_suite"] == "ed25519-seed"
    decoded = base64.urlsafe_b64decode(p["space_seed"])
    assert len(decoded) == 32
    # It matches the locally-stored seed.
    assert decoded == await stack.space_repo.get_space_seed(space.id)


async def test_promote_remote_admin_no_share_when_delegation_off(stack):
    """Delegation OFF → promotion still federates the role, but NO seed
    share is sent."""
    from socialhome.domain.space import SpaceFeatures, SpaceRole

    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="S",
        features=SpaceFeatures(delegated_admin_authority=False),
    )
    fed = _seed_share_fed()
    stack.space_svc._federation = fed
    remote = await _wire_remote_members(stack)
    await remote.add(
        space_id=space.id,
        instance_id="peer-x",
        user_id="ru1",
        user_pk=None,
        display_name=None,
    )

    await stack.space_svc.set_remote_member_role(
        space.id,
        actor_username="anna",
        instance_id="peer-x",
        user_id="ru1",
        role=SpaceRole.ADMIN,
    )

    fed.send_with_mesh_fallback.assert_not_awaited()


async def test_promote_local_admin_sends_no_seed(stack):
    """A LOCAL admin's household already holds the seed — promoting a
    local member via set_role sends no SPACE_ADMIN_KEY_SHARE."""
    from socialhome.domain.space import SpaceFeatures, SpaceRole

    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="S",
        features=SpaceFeatures(delegated_admin_authority=True),
    )
    await stack.space_svc.add_member(
        space.id, actor_username="anna", user_id=bob.user_id
    )
    fed = _seed_share_fed()
    stack.space_svc._federation = fed
    await _wire_remote_members(stack)

    await stack.space_svc.set_role(
        space.id, actor_username="anna", user_id=bob.user_id, role=SpaceRole.ADMIN
    )

    fed.send_with_mesh_fallback.assert_not_awaited()


async def test_promote_remote_admin_skips_share_for_old_peer(stack, caplog):
    """A sub-v_22 admin household → the role still federates, but the seed
    share is SKIPPED (no send), logged at WARNING — the admin just can't
    act offline yet."""
    import logging

    from socialhome.domain.space import SpaceFeatures, SpaceRole

    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="S",
        features=SpaceFeatures(delegated_admin_authority=True),
    )
    fed = _seed_share_fed(supports=False)
    stack.space_svc._federation = fed
    remote = await _wire_remote_members(stack)
    await remote.add(
        space_id=space.id,
        instance_id="peer-old",
        user_id="ru1",
        user_pk=None,
        display_name=None,
    )

    with caplog.at_level(logging.WARNING, logger="socialhome.services.space_service"):
        await stack.space_svc.set_remote_member_role(
            space.id,
            actor_username="anna",
            instance_id="peer-old",
            user_id="ru1",
            role=SpaceRole.ADMIN,
        )

    fed.send_with_mesh_fallback.assert_not_awaited()
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_flag_flip_distributes_seed_to_all_remote_admins(stack):
    """Flipping delegated_admin_authority False→True on an owned space
    distributes the seed to EVERY current remote admin household (once
    each), but not to remote plain members."""
    from socialhome.domain.federation import FederationEventType
    from socialhome.domain.space import SpaceFeatures, SpaceRole

    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="S",
        features=SpaceFeatures(delegated_admin_authority=False),
    )
    fed = _seed_share_fed()
    stack.space_svc._federation = fed
    remote = await _wire_remote_members(stack)
    for inst, uid, role in [
        ("peer-a", "u1", SpaceRole.ADMIN),
        ("peer-a", "u2", SpaceRole.ADMIN),  # second admin, same household
        ("peer-b", "u3", SpaceRole.ADMIN),
        ("peer-c", "u4", SpaceRole.MEMBER),  # plain member — excluded
    ]:
        await remote.add(
            space_id=space.id,
            instance_id=inst,
            user_id=uid,
            user_pk=None,
            display_name=None,
        )
        await remote.set_role(space.id, inst, uid, role)

    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(delegated_admin_authority=True),
    )

    targets = {
        c.kwargs["to_instance_id"]
        for c in fed.send_with_mesh_fallback.await_args_list
        if c.kwargs["event_type"] is FederationEventType.SPACE_ADMIN_KEY_SHARE
    }
    assert targets == {"peer-a", "peer-b"}


async def test_flag_flip_true_to_false_sends_nothing(stack):
    """Turning delegation OFF does NOT send (already-shared seeds persist;
    deeper revocation is a later phase)."""
    from socialhome.domain.federation import FederationEventType
    from socialhome.domain.space import SpaceFeatures, SpaceRole

    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="S",
        features=SpaceFeatures(delegated_admin_authority=True),
    )
    fed = _seed_share_fed()
    stack.space_svc._federation = fed
    remote = await _wire_remote_members(stack)
    await remote.add(
        space_id=space.id,
        instance_id="peer-a",
        user_id="u1",
        user_pk=None,
        display_name=None,
    )
    await remote.set_role(space.id, "peer-a", "u1", SpaceRole.ADMIN)

    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(delegated_admin_authority=False),
    )

    shares = [
        c
        for c in fed.send_with_mesh_fallback.await_args_list
        if c.kwargs.get("event_type") is FederationEventType.SPACE_ADMIN_KEY_SHARE
    ]
    assert shares == []


# ─── Space roster gossip — outbound (v_23) ─────────────────────────────


def _roster_gossip_fed():
    """AsyncMock federation service wired for the roster-gossip broadcast
    assertions (broadcast_to_space_members captures every gossip call)."""
    from unittest.mock import AsyncMock

    fed = AsyncMock()
    fed.peer_supports = AsyncMock(return_value=True)
    fed.broadcast_to_space_members = AsyncMock()
    return fed


def _gossip_calls(fed, event_type):
    """Filter broadcast_to_space_members calls down to one event type."""
    return [
        c
        for c in fed.broadcast_to_space_members.await_args_list
        if c.args[1] is event_type
    ]


async def test_add_member_broadcasts_signed_joined(stack):
    """Seating a local member broadcasts a SPACE_MEMBER_JOINED to member
    households, gated on the roster-gossip capability, with a payload that
    carries a monotonic member_version + a valid authority signature."""
    from socialhome.crypto import verify_ed25519
    from socialhome.domain.federation import FederationEventType
    from socialhome.domain.federation_capabilities import FederationCapability
    from socialhome.services.space_crypto_service import (
        authority_signing_bytes,
        strip_authority_sig_fields,
    )

    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    fed = _roster_gossip_fed()
    stack.space_svc._federation = fed

    await stack.space_svc.add_member(
        space.id, actor_username="anna", user_id=bob.user_id
    )

    joined = _gossip_calls(fed, FederationEventType.SPACE_MEMBER_JOINED)
    assert len(joined) == 1
    call = joined[0]
    assert call.args[0] == space.id
    # Gated on the roster-gossip capability.
    assert (
        call.kwargs.get("min_proto_version")
        == FederationCapability.MIN_FOR_SPACE_ROSTER_GOSSIP
    )
    p = call.args[2]
    assert p["space_id"] == space.id
    assert p["user_id"] == bob.user_id
    assert p["instance_id"] == stack.iid
    assert p["role"] == "member"
    assert isinstance(p["member_version"], int) and p["member_version"] >= 1
    assert "roster_version" in p
    # Authority signature verifies against the space's public key, over the
    # payload with the two sig fields stripped.
    space_row = await stack.space_repo.get(space.id)
    pub = bytes.fromhex(space_row.identity_public_key)
    from socialhome.crypto import b64url_decode

    sig = b64url_decode(p["authority_sig"])
    bare = strip_authority_sig_fields(p)
    msg = authority_signing_bytes(
        event_type=FederationEventType.SPACE_MEMBER_JOINED.value,
        space_id=space.id,
        payload=bare,
    )
    assert verify_ed25519(pub, msg, sig) is True


async def test_remove_member_broadcasts_signed_left(stack):
    """Removing a local member broadcasts a SPACE_MEMBER_LEFT gossip."""
    from socialhome.domain.federation import FederationEventType

    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(
        space.id, actor_username="anna", user_id=bob.user_id
    )
    fed = _roster_gossip_fed()
    stack.space_svc._federation = fed

    await stack.space_svc.remove_member(
        space.id, actor_username="anna", user_id=bob.user_id
    )

    left = _gossip_calls(fed, FederationEventType.SPACE_MEMBER_LEFT)
    assert len(left) == 1
    p = left[0].args[2]
    assert p["space_id"] == space.id
    assert p["user_id"] == bob.user_id
    assert p["authority_sig"]


async def test_member_version_is_monotonic_across_emits(stack):
    """Two roster mutations for the same user emit strictly increasing
    member_version values (the convergence-merge ordering source)."""
    from socialhome.domain.federation import FederationEventType
    from socialhome.domain.space import SpaceRole

    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    fed = _roster_gossip_fed()
    stack.space_svc._federation = fed

    await stack.space_svc.add_member(
        space.id, actor_username="anna", user_id=bob.user_id
    )
    await stack.space_svc.set_role(
        space.id, actor_username="anna", user_id=bob.user_id, role=SpaceRole.ADMIN
    )

    joined = _gossip_calls(fed, FederationEventType.SPACE_MEMBER_JOINED)
    assert len(joined) == 2  # add + role-change both emit JOINED
    v1 = joined[0].args[2]["member_version"]
    v2 = joined[1].args[2]["member_version"]
    assert v2 > v1
    # The role-change JOINED carries the new role (upsert semantics).
    assert joined[1].args[2]["role"] == SpaceRole.ADMIN


async def test_set_remote_member_role_broadcasts_joined(stack):
    """Changing a remote member's role broadcasts a SPACE_MEMBER_JOINED gossip
    carrying the new role (the join event doubles as the role upsert)."""
    from socialhome.domain.federation import FederationEventType
    from socialhome.domain.space import SpaceRole

    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    fed = _roster_gossip_fed()
    stack.space_svc._federation = fed
    remote = await _wire_remote_members(stack)
    await remote.add(
        space_id=space.id,
        instance_id="peer-x",
        user_id="ru1",
        user_pk=None,
        display_name="R",
    )

    await stack.space_svc.set_remote_member_role(
        space.id,
        actor_username="anna",
        instance_id="peer-x",
        user_id="ru1",
        role=SpaceRole.ADMIN,
    )

    joined = _gossip_calls(fed, FederationEventType.SPACE_MEMBER_JOINED)
    assert len(joined) == 1
    p = joined[0].args[2]
    assert p["user_id"] == "ru1"
    assert p["instance_id"] == "peer-x"
    assert p["role"] == SpaceRole.ADMIN


async def test_remove_remote_member_broadcasts_left(stack):
    """Kicking a remote member broadcasts a SPACE_MEMBER_LEFT gossip."""
    from unittest.mock import AsyncMock

    from socialhome.domain.federation import DeliveryResult, FederationEventType

    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    fed = _roster_gossip_fed()
    fed.send_with_mesh_fallback = AsyncMock(
        return_value=DeliveryResult(instance_id="peer-x", ok=True)
    )
    stack.space_svc._federation = fed
    from unittest.mock import MagicMock

    stack.space_svc._federation_repo = MagicMock()
    remote = await _wire_remote_members(stack)
    await remote.add(
        space_id=space.id,
        instance_id="peer-x",
        user_id="ru1",
        user_pk=None,
        display_name="R",
    )

    await stack.space_svc.remove_remote_member(
        space.id, actor_username="anna", instance_id="peer-x", user_id="ru1"
    )

    left = _gossip_calls(fed, FederationEventType.SPACE_MEMBER_LEFT)
    assert len(left) == 1
    p = left[0].args[2]
    assert p["user_id"] == "ru1"
    assert p["instance_id"] == "peer-x"


async def test_no_seed_skips_gossip_gracefully(stack):
    """A space we don't own (no signing seed) skips signing + gossip rather
    than crashing — falls back to today's host-only behaviour."""
    from socialhome.domain.federation import FederationEventType

    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    # Simulate a non-owned space (seed NULL + owner elsewhere) so
    # ensure_space_seed returns None and the gossip emit must no-op.
    await stack.db.enqueue(
        "UPDATE spaces SET identity_private_key=NULL, owner_instance_id=? WHERE id=?",
        ("some-other-host", space.id),
    )
    fed = _roster_gossip_fed()
    stack.space_svc._federation = fed

    # add_member requires admin; anna is still owner_username locally so the
    # guard passes — the gossip emit is what must no-op.
    await stack.space_svc.add_member(
        space.id, actor_username="anna", user_id=bob.user_id
    )

    assert _gossip_calls(fed, FederationEventType.SPACE_MEMBER_JOINED) == []


# ─── Membership ops offline-of-owner — delegated-admin gate (Phase 3) ──────


def _invite_fed():
    """AsyncMock federation service wired for invite + gossip assertions.

    ``send_with_mesh_fallback`` (used by ``_send_invite_envelope``) returns a
    successful DeliveryResult; ``broadcast_to_space_members`` captures the
    authority gossip; ``peer_supports`` is True so the gossip isn't gated out.
    """
    from unittest.mock import AsyncMock, MagicMock

    from socialhome.domain.federation import DeliveryResult

    fed = AsyncMock()
    fed.send_with_mesh_fallback = AsyncMock(
        return_value=DeliveryResult(instance_id="peer", ok=True)
    )
    fed.broadcast_to_space_members = AsyncMock()
    fed.peer_supports = AsyncMock(return_value=True)
    return fed, MagicMock()


async def _make_delegated_admin_space(stack, *, delegation: bool):
    """Seat a local ADMIN of a space whose owner household is REMOTE.

    Builds the offline-of-owner shape: create the space locally (mints a real
    Ed25519 seed + matching pubkey), then flip ``owner_instance_id`` to a
    remote host and ``delegated_admin_authority`` to ``delegation``. The local
    actor ``"admin"`` is seated as a non-owner ADMIN; the stored seed stays put
    so ``ensure_space_seed`` returns it (delegated key-share simulated).

    Returns the reloaded :class:`Space`.
    """
    from socialhome.domain.space import SpaceMember, SpaceRole

    admin = await stack.provision_user("deladmin")
    space = await stack.space_svc.create_space(owner_username="deladmin", name="S")
    # The owner is actually a remote household; we are a delegated admin.
    await stack.db.enqueue(
        "UPDATE spaces SET owner_instance_id=?, delegated_admin_authority=? WHERE id=?",
        ("remote-owner-host", int(delegation), space.id),
    )
    # Re-seat the local actor as a non-owner ADMIN (create_space made them OWNER).
    await stack.space_repo.save_member(
        SpaceMember(
            space_id=space.id,
            user_id=admin.user_id,
            role=SpaceRole.ADMIN,
            joined_at="2026-01-01T00:00:00+00:00",
        )
    )
    reloaded = await stack.space_repo.get(space.id)
    # Sanity: we hold the seed (Phase-1 share simulated) but are not the host.
    assert await stack.space_repo.get_space_seed(space.id) is not None
    assert reloaded.owner_instance_id != stack.iid
    return reloaded


async def test_offline_owner_delegated_admin_invite_works(stack):
    """A non-owner ADMIN who holds the seed in a delegation-ON space can invite
    a remote user (owner offline) — the invite envelope ships the content key,
    and the seat-time JOINED gossip is authority-signed (verifies against the
    space pubkey)."""
    from socialhome.crypto import b64url_decode
    from socialhome.domain.federation import FederationEventType
    from socialhome.services.space_crypto_service import verify_authority_event

    from unittest.mock import AsyncMock

    space = await _make_delegated_admin_space(stack, delegation=True)
    fed, fed_repo = _invite_fed()
    stack.space_svc.attach_federation(
        federation_service=fed,
        federation_repo=fed_repo,
        remote_member_repo=(await _wire_remote_members(stack)),
    )
    # Attach a crypto service so the snapshot embeds the content key — the
    # delegated admin holds it (Phase-1) and hands it to the invitee.
    crypto = AsyncMock()
    crypto.export_current_key = AsyncMock(return_value=(7, bytes(range(32))))
    stack.space_svc.attach_space_crypto_service(crypto)

    token = await stack.space_svc.invite_remote_user(
        space.id,
        actor_username="deladmin",
        invitee_instance_id="peer",
        invitee_user_id="bob",
    )
    assert token
    # The invite envelope was sent and carries the content key in space_meta.
    fed.send_with_mesh_fallback.assert_awaited_once()
    payload = fed.send_with_mesh_fallback.await_args.kwargs["payload"]
    assert payload["invitee_user_id"] == "bob"
    assert payload["invite_token"] == token
    meta = payload["space_meta"]
    # build_space_snapshot_for_federation embeds the current content key.
    assert "space_content_key" in meta
    assert meta["space_content_key"]["epoch"] == 7

    # Now the host-side accept seats the member and gossips a SIGNED JOINED.
    await stack.space_svc.broadcast_remote_member_joined(
        space.id,
        instance_id="peer",
        user_id="bob",
        user_pk=None,
        display_name="Bob",
    )
    joined = _gossip_calls(fed, FederationEventType.SPACE_MEMBER_JOINED)
    assert len(joined) == 1
    p = joined[0].args[2]
    assert p["user_id"] == "bob"
    pub = bytes.fromhex((await stack.space_repo.get(space.id)).identity_public_key)
    assert (
        verify_authority_event(
            event_type=FederationEventType.SPACE_MEMBER_JOINED.value,
            space_id=space.id,
            payload={
                k: v
                for k, v in p.items()
                if k not in ("authority_sig", "authority_sig_suite")
            },
            authority_sig=p["authority_sig"],
            authority_sig_suite=p["authority_sig_suite"],
            space_public_key=pub,
        )
        is True
    )
    # Belt-and-suspenders: the b64url sig decodes to a 64-byte Ed25519 sig.
    assert len(b64url_decode(p["authority_sig"])) == 64


async def test_offline_owner_delegated_admin_invite_gated_when_off(stack):
    """The SAME non-owner ADMIN in a delegation-OFF space cannot mint locally —
    instead the invite is FORWARDED to the host as an owner-approval request
    (Phase 6). ``invite_remote_user`` returns "" and a SPACE_REMOTE_ADMIN_ACTION
    with action="invite" + the invitee params was sent to the host. No local
    SPACE_PRIVATE_INVITE envelope is minted here."""
    from socialhome.domain.federation import FederationEventType

    space = await _make_delegated_admin_space(stack, delegation=False)
    fed, fed_repo = _invite_fed()
    stack.space_svc.attach_federation(
        federation_service=fed,
        federation_repo=fed_repo,
        remote_member_repo=(await _wire_remote_members(stack)),
    )

    token = await stack.space_svc.invite_remote_user(
        space.id,
        actor_username="deladmin",
        invitee_instance_id="peer",
        invitee_user_id="bob",
    )
    # No local token minted — the invite was forwarded for owner approval.
    assert token == ""
    fed.send_with_mesh_fallback.assert_awaited_once()
    call = fed.send_with_mesh_fallback.await_args
    assert call.kwargs["event_type"] is (FederationEventType.SPACE_REMOTE_ADMIN_ACTION)
    assert call.kwargs["to_instance_id"] == "remote-owner-host"
    payload = call.kwargs["payload"]
    assert payload["action"] == "invite"
    assert payload["params"] == {
        "invitee_instance_id": "peer",
        "invitee_user_id": "bob",
    }
    assert payload["space_id"] == space.id


async def test_owner_invite_unaffected_by_flag(stack):
    """The OWNING household can invite regardless of the flag (OFF and ON)."""

    async def _owner_can_invite(delegation: bool):
        await stack.provision_user("anna")
        space = await stack.space_svc.create_space(owner_username="anna", name="S")
        await stack.db.enqueue(
            "UPDATE spaces SET delegated_admin_authority=? WHERE id=?",
            (int(delegation), space.id),
        )
        fed, fed_repo = _invite_fed()
        stack.space_svc.attach_federation(
            federation_service=fed,
            federation_repo=fed_repo,
            remote_member_repo=(await _wire_remote_members(stack)),
        )
        token = await stack.space_svc.invite_remote_user(
            space.id,
            actor_username="anna",
            invitee_instance_id="peer",
            invitee_user_id="bob",
        )
        assert token
        fed.send_with_mesh_fallback.assert_awaited_once()

    await _owner_can_invite(delegation=False)
    await _owner_can_invite(delegation=True)


async def test_offline_seated_owner_converges_via_admin_gossip(stack):
    """An OWNER household that was offline at seat-time applies the delegated
    admin's authority-signed SPACE_MEMBER_JOINED on receipt and ends with the
    new member in its roster — even though it never processed the accept."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from socialhome.crypto import generate_space_keypair
    from socialhome.domain.federation import FederationEventType
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )
    from socialhome.federation.private_invite_handler import (
        PrivateSpaceInviteHandler,
    )
    from socialhome.repositories.space_remote_member_repo import (
        SqliteSpaceRemoteMemberRepo,
    )
    from socialhome.services.space_crypto_service import (
        sign_authority_event,
        strip_authority_sig_fields,
    )

    # The owner household holds the space with its public key (it IS the host).
    kp = generate_space_keypair()
    space_id = "sp-converge"
    await stack.space_repo.save(
        Space(
            id=space_id,
            name="S",
            owner_instance_id=stack.iid,  # owner = this household
            owner_username="anna",
            identity_public_key=kp.public_key.hex(),
            config_sequence=0,
            features=SpaceFeatures(delegated_admin_authority=True),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    remote = SqliteSpaceRemoteMemberRepo(stack.db)
    handler = PrivateSpaceInviteHandler(
        bus=AsyncMock(),
        space_repo=stack.space_repo,
        remote_member_repo=remote,
    )

    # The delegated admin (a DIFFERENT household) signs a JOINED with the seed.
    bare = {
        "space_id": space_id,
        "user_id": "bob",
        "instance_id": "invitee-host",
        "display_name": "Bob",
        "user_pk": None,
        "role": "member",
        "member_version": 7,
        "roster_version": 7,
    }
    signed = sign_authority_event(
        event_type=FederationEventType.SPACE_MEMBER_JOINED.value,
        space_id=space_id,
        payload=strip_authority_sig_fields(bare),
        space_seed=kp.private_key,
    )
    event = SimpleNamespace(
        event_type=FederationEventType.SPACE_MEMBER_JOINED,
        payload={**bare, **signed},
        from_instance="delegated-admin-host",  # NOT the owner — relayed
        space_id=space_id,
    )

    # Owner was offline at seat-time: it never saw the accept. It now receives
    # the admin's gossip and converges.
    assert await remote.get(space_id, "invitee-host", "bob") is None
    await handler._on_space_member_joined(event)
    seated = await remote.get(space_id, "invitee-host", "bob")
    assert seated is not None
    assert seated.member_version == 7
    assert seated.tombstoned is False


async def test_delegated_no_seed_gossip_warns(stack, caplog):
    """A delegation-ON space whose host isn't us but for which we hold no seed
    is an anomaly (Phase-1 share missing) — the gossip skip is logged at
    WARNING (still graceful, no crash, no broadcast)."""
    import logging

    from socialhome.domain.federation import FederationEventType

    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    # Delegation ON, owner is REMOTE, and we hold NO seed — the anomaly.
    await stack.db.enqueue(
        "UPDATE spaces SET identity_private_key=NULL, owner_instance_id=?, "
        "delegated_admin_authority=1 WHERE id=?",
        ("remote-owner-host", space.id),
    )
    fed = _roster_gossip_fed()
    stack.space_svc._federation = fed

    with caplog.at_level(logging.WARNING, logger="socialhome.services.space_service"):
        await stack.space_svc.add_member(
            space.id, actor_username="anna", user_id=bob.user_id
        )

    # No gossip broadcast (graceful skip), but a diagnosable WARNING fired.
    assert _gossip_calls(fed, FederationEventType.SPACE_MEMBER_JOINED) == []
    assert any(
        r.levelno == logging.WARNING and space.id in r.getMessage()
        for r in caplog.records
    )


async def test_owned_no_seed_gossip_stays_silent(stack, caplog):
    """For an owner-local space with no seed the skip stays silent (today's
    behaviour) — the WARNING is reserved for the delegated anomaly."""
    import logging

    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    # Non-owned + delegation OFF → no warning expected (the silent fallback).
    await stack.db.enqueue(
        "UPDATE spaces SET identity_private_key=NULL, owner_instance_id=? WHERE id=?",
        ("remote-owner-host", space.id),
    )
    fed = _roster_gossip_fed()
    stack.space_svc._federation = fed

    with caplog.at_level(logging.WARNING, logger="socialhome.services.space_service"):
        await stack.space_svc.add_member(
            space.id, actor_username="anna", user_id=bob.user_id
        )

    assert not any(
        r.levelno == logging.WARNING and "no signing seed" in r.getMessage()
        for r in caplog.records
    )


# ─── Phase 6a — owner-approval gate for SPACE_REMOTE_ADMIN_ACTION ──────────


async def _host_space_with_remote_admin(stack, *, delegation, admin=True):
    """Host-owned space + a remote member seated on instance-A.

    ``delegation`` flips ``delegated_admin_authority``; ``admin`` decides
    whether the remote actor is seated as an admin (vs a plain member)."""
    from socialhome.domain.space import SpaceFeatures, SpaceRole

    await stack.provision_user("alicehost")
    space = await stack.space_svc.create_space(
        owner_username="alicehost",
        name="S",
        features=SpaceFeatures(delegated_admin_authority=delegation),
    )
    remote = await _wire_remote_members(stack)
    await remote.add(
        space_id=space.id,
        instance_id="instance-A",
        user_id="u-admin",
        user_pk=None,
        display_name=None,
    )
    if admin:
        await remote.set_role(space.id, "instance-A", "u-admin", SpaceRole.ADMIN)
    return space


async def test_remote_admin_action_executes_when_delegation_on(stack):
    """Delegation ON + a valid remote admin → EXECUTED and the action ran."""
    from socialhome.domain.space import RemoteAdminOutcome

    space = await _host_space_with_remote_admin(stack, delegation=True)
    outcome = await stack.space_svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="update_config",
        params={"name": "From Remote Admin"},
    )
    assert outcome is RemoteAdminOutcome.EXECUTED
    refreshed = await stack.space_repo.get(space.id)
    assert refreshed.name == "From Remote Admin"


async def test_remote_admin_action_needs_approval_when_delegation_off(stack):
    """Delegation OFF + a valid remote admin → NEEDS_OWNER_APPROVAL and the
    action did NOT run (config unchanged)."""
    from socialhome.domain.space import RemoteAdminOutcome

    space = await _host_space_with_remote_admin(stack, delegation=False)
    outcome = await stack.space_svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="update_config",
        params={"name": "Should Not Apply"},
    )
    assert outcome is RemoteAdminOutcome.NEEDS_OWNER_APPROVAL
    refreshed = await stack.space_repo.get(space.id)
    assert refreshed.name == "S"


async def test_remote_admin_action_dropped_for_non_admin(stack):
    """A non-admin remote actor → DROPPED with no execution, regardless of
    the delegation flag."""
    from socialhome.domain.space import RemoteAdminOutcome

    for delegation in (True, False):
        space = await _host_space_with_remote_admin(
            stack, delegation=delegation, admin=False
        )
        outcome = await stack.space_svc.apply_remote_admin_action(
            space.id,
            actor_instance_id="instance-A",
            actor_user_id="u-admin",
            action="update_config",
            params={"name": "Hacked"},
        )
        assert outcome is RemoteAdminOutcome.DROPPED
        refreshed = await stack.space_repo.get(space.id)
        assert refreshed.name == "S"


async def test_remote_admin_action_dropped_when_not_hosted_here(stack):
    """A space hosted on another household → DROPPED, even with a seated
    admin and delegation ON."""
    from socialhome.domain.space import RemoteAdminOutcome

    space = await _host_space_with_remote_admin(stack, delegation=True)
    await stack.db.enqueue(
        "UPDATE spaces SET owner_instance_id=? WHERE id=?",
        ("some-other-household", space.id),
    )
    outcome = await stack.space_svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="archive",
        params={},
    )
    assert outcome is RemoteAdminOutcome.DROPPED
    refreshed = await stack.space_repo.get(space.id)
    assert refreshed.archived is False


async def test_apply_approved_admin_action_runs_as_owner(stack):
    """apply_approved_admin_action executes the held action as the owner on a
    hosted space (the owner-approval gate is assumed already passed)."""
    victim = await stack.provision_user("victimlocal")
    space = await _host_space_with_remote_admin(stack, delegation=False)
    await stack.space_svc.add_member(
        space.id, actor_username="alicehost", user_id=victim.user_id, role="member"
    )
    await stack.space_svc.apply_approved_admin_action(
        space.id,
        action="ban",
        params={"user_id": victim.user_id, "reason": "approved"},
    )
    assert await stack.space_repo.get_member(space.id, victim.user_id) is None
    assert await stack.space_repo.is_banned(space.id, victim.user_id) is True


async def test_apply_approved_admin_action_noop_when_not_hosted_here(stack):
    """apply_approved_admin_action is a no-op when the space is not hosted
    here — it never runs the action against a non-authoritative stub."""
    victim = await stack.provision_user("victimlocal")
    space = await _host_space_with_remote_admin(stack, delegation=False)
    await stack.space_svc.add_member(
        space.id, actor_username="alicehost", user_id=victim.user_id, role="member"
    )
    await stack.db.enqueue(
        "UPDATE spaces SET owner_instance_id=? WHERE id=?",
        ("some-other-household", space.id),
    )
    await stack.space_svc.apply_approved_admin_action(
        space.id,
        action="ban",
        params={"user_id": victim.user_id},
    )
    # Member still present, not banned — the no-op held.
    assert await stack.space_repo.get_member(space.id, victim.user_id) is not None
    assert await stack.space_repo.is_banned(space.id, victim.user_id) is False


async def test_remote_admin_update_config_cannot_flip_delegation_flag(stack):
    """H1: a forwarded update_config that flips delegated_admin_authority must
    NOT change the owner-only flag, even with delegation ON (self-authorized).
    A benign field in the same edit still applies, proving the edit ran."""
    from socialhome.domain.space import RemoteAdminOutcome, SpaceFeatures

    space = await _host_space_with_remote_admin(stack, delegation=True)
    assert space.features.delegated_admin_authority is True
    # The wire tries to REVOKE delegation (True -> False) while also changing
    # a benign feature (calendar_enabled) and the name.
    wire = SpaceFeatures(delegated_admin_authority=False, location=True).to_wire_dict()
    outcome = await stack.space_svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="update_config",
        params={"name": "Benign Rename", "features": wire},
    )
    assert outcome is RemoteAdminOutcome.EXECUTED
    refreshed = await stack.space_repo.get(space.id)
    # The owner-only flag is pinned to its current value, NOT the wire's.
    assert refreshed.features.delegated_admin_authority is True
    # …but the rest of the edit applied.
    assert refreshed.name == "Benign Rename"
    assert refreshed.features.location is True


async def test_approved_admin_update_config_cannot_flip_delegation_flag(stack):
    """H1 (OFF -> owner-approved path): an approved forwarded update_config
    carrying a flipped delegated_admin_authority leaves the flag unchanged."""
    from socialhome.domain.space import SpaceFeatures

    space = await _host_space_with_remote_admin(stack, delegation=False)
    assert space.features.delegated_admin_authority is False
    # The wire tries to GRANT delegation (False -> True).
    wire = SpaceFeatures(delegated_admin_authority=True, location=True).to_wire_dict()
    await stack.space_svc.apply_approved_admin_action(
        space.id,
        action="update_config",
        params={"name": "Approved Rename", "features": wire},
    )
    refreshed = await stack.space_repo.get(space.id)
    assert refreshed.features.delegated_admin_authority is False
    assert refreshed.name == "Approved Rename"
    assert refreshed.features.location is True


async def test_remote_admin_action_unknown_action_dropped(stack):
    """H2: a non-forwardable action (e.g. dissolve) is DROPPED at the door for
    both delegation states — never NEEDS_OWNER_APPROVAL or EXECUTED — and ON
    causes no mutation."""
    from socialhome.domain.space import RemoteAdminOutcome

    for delegation in (True, False):
        space = await _host_space_with_remote_admin(stack, delegation=delegation)
        outcome = await stack.space_svc.apply_remote_admin_action(
            space.id,
            actor_instance_id="instance-A",
            actor_user_id="u-admin",
            action="dissolve",
            params={},
        )
        assert outcome is RemoteAdminOutcome.DROPPED
        refreshed = await stack.space_repo.get(space.id)
        assert refreshed.name == "S"
        assert refreshed.archived is False


async def test_invite_is_a_forwardable_admin_action():
    """ "invite" must be in the forwardable allow-list so a forwarded invite is
    not DROPPED at the host's door."""
    from socialhome.services.space_service import SpaceService

    assert "invite" in SpaceService._FORWARDABLE_ADMIN_ACTIONS


async def test_remote_admin_invite_needs_approval_when_delegation_off(stack):
    """Host side: a forwarded invite from a remote admin in a delegation-OFF
    space → NEEDS_OWNER_APPROVAL and NO SPACE_PRIVATE_INVITE was minted."""
    from socialhome.domain.space import RemoteAdminOutcome

    space = await _host_space_with_remote_admin(stack, delegation=False)
    fed, fed_repo = _invite_fed()
    stack.space_svc.attach_federation(
        federation_service=fed,
        federation_repo=fed_repo,
        remote_member_repo=(await _wire_remote_members(stack)),
    )
    outcome = await stack.space_svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="invite",
        params={"invitee_instance_id": "peer", "invitee_user_id": "bob"},
    )
    assert outcome is RemoteAdminOutcome.NEEDS_OWNER_APPROVAL
    fed.send_with_mesh_fallback.assert_not_awaited()


async def test_remote_admin_invite_executes_when_delegation_on(stack):
    """Host side: a forwarded invite from a remote admin in a delegation-ON
    space → EXECUTED and a SPACE_PRIVATE_INVITE was sent to the invitee, run as
    owner (no re-entry into the OFF forward branch)."""
    from socialhome.domain.federation import FederationEventType
    from socialhome.domain.space import RemoteAdminOutcome

    space = await _host_space_with_remote_admin(stack, delegation=True)
    fed, fed_repo = _invite_fed()
    stack.space_svc.attach_federation(
        federation_service=fed,
        federation_repo=fed_repo,
        remote_member_repo=(await _wire_remote_members(stack)),
    )
    outcome = await stack.space_svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="invite",
        params={"invitee_instance_id": "peer", "invitee_user_id": "bob"},
    )
    assert outcome is RemoteAdminOutcome.EXECUTED
    fed.send_with_mesh_fallback.assert_awaited_once()
    call = fed.send_with_mesh_fallback.await_args
    assert call.kwargs["event_type"] is FederationEventType.SPACE_PRIVATE_INVITE
    assert call.kwargs["to_instance_id"] == "peer"
    assert call.kwargs["payload"]["invitee_user_id"] == "bob"


async def test_apply_approved_invite_mints_as_owner(stack):
    """apply_approved_admin_action(action="invite") mints the invite as owner —
    a SPACE_PRIVATE_INVITE envelope is sent to the invitee household."""
    from socialhome.domain.federation import FederationEventType

    space = await _host_space_with_remote_admin(stack, delegation=False)
    fed, fed_repo = _invite_fed()
    stack.space_svc.attach_federation(
        federation_service=fed,
        federation_repo=fed_repo,
        remote_member_repo=(await _wire_remote_members(stack)),
    )
    await stack.space_svc.apply_approved_admin_action(
        space.id,
        action="invite",
        params={"invitee_instance_id": "peer", "invitee_user_id": "bob"},
    )
    fed.send_with_mesh_fallback.assert_awaited_once()
    call = fed.send_with_mesh_fallback.await_args
    assert call.kwargs["event_type"] is FederationEventType.SPACE_PRIVATE_INVITE
    assert call.kwargs["to_instance_id"] == "peer"
    assert call.kwargs["payload"]["invitee_user_id"] == "bob"


async def test_remote_admin_invite_missing_params_noop(stack):
    """Host side: a forwarded invite missing invitee params is a no-op in
    _run_admin_action (delegation ON) — no envelope minted."""
    from socialhome.domain.space import RemoteAdminOutcome

    space = await _host_space_with_remote_admin(stack, delegation=True)
    fed, fed_repo = _invite_fed()
    stack.space_svc.attach_federation(
        federation_service=fed,
        federation_repo=fed_repo,
        remote_member_repo=(await _wire_remote_members(stack)),
    )
    outcome = await stack.space_svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="invite",
        params={"invitee_instance_id": "", "invitee_user_id": ""},
    )
    assert outcome is RemoteAdminOutcome.EXECUTED
    fed.send_with_mesh_fallback.assert_not_awaited()
