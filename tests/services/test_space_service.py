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
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    space_repo = SqliteSpaceRepo(db)
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


async def test_update_config_accepts_retention_exempt_types(stack):
    """retention_exempt_types round-trips through the repo."""
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
    federation.broadcast_to_space_members.assert_awaited_once()
    call = federation.broadcast_to_space_members.call_args
    assert call.args[1] is FederationEventType.SPACE_KEY_EXCHANGE_REKEY
    payload = call.args[2]
    assert payload["space_id"] == space.id
    assert payload["space_content_key"]["epoch"] == 7
    assert payload["space_content_key"]["key_suite"] == "aesgcm-256"


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
    federation.broadcast_to_space_members.assert_awaited_once()
    assert (
        federation.broadcast_to_space_members.call_args.args[1]
        is FederationEventType.SPACE_KEY_EXCHANGE_REKEY
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
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    space_repo = SqliteSpaceRepo(db)
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
