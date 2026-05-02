"""Tests for GalleryService."""

from __future__ import annotations


import pytest

from socialhome.config import Config
from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.gallery_repo import SqliteGalleryRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.gallery_service import (
    DESCRIPTION_MAX,
    GalleryNotFoundError,
    GalleryPermissionError,
    GalleryService,
    NAME_MAX,
)


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    for username, uid, admin in [
        ("alice", "a-id", 1),
        ("bob", "b-id", 0),
        ("eve", "e-id", 0),
    ]:
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin)"
            " VALUES(?,?,?,?)",
            (username, uid, username.title(), admin),
        )
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-1', 'X', ?, 'alice', ?)",
        (iid, "ab" * 32),
    )
    # alice = owner, bob = member (no special role).
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES('sp-1', 'a-id', 'owner')",
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES('sp-1', 'b-id', 'member')",
    )
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "t.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
    )
    svc = GalleryService(
        SqliteGalleryRepo(db),
        SqliteSpaceRepo(db),
        EventBus(),
        cfg,
    )
    yield svc
    await db.shutdown()


# ─── Album CRUD permissions ──────────────────────────────────────────────


async def test_create_album_member_succeeds(env):
    a = await env.create_album(
        space_id="sp-1",
        owner_user_id="b-id",
        name="Trip 2026",
    )
    assert a.name == "Trip 2026"
    assert a.owner_user_id == "b-id"


async def test_create_album_non_member_403(env):
    with pytest.raises(GalleryPermissionError):
        await env.create_album(
            space_id="sp-1",
            owner_user_id="e-id",
            name="Hostile",
        )


async def test_create_household_album_no_membership_check(env):
    a = await env.create_album(
        space_id=None,
        owner_user_id="e-id",
        name="Personal",
    )
    assert a.space_id is None


async def test_create_album_empty_name_422(env):
    with pytest.raises(ValueError):
        await env.create_album(
            space_id="sp-1",
            owner_user_id="a-id",
            name="",
        )


async def test_create_album_too_long_name_422(env):
    with pytest.raises(ValueError):
        await env.create_album(
            space_id="sp-1",
            owner_user_id="a-id",
            name="x" * (NAME_MAX + 1),
        )


async def test_create_album_too_long_description_422(env):
    with pytest.raises(ValueError):
        await env.create_album(
            space_id="sp-1",
            owner_user_id="a-id",
            name="X",
            description="x" * (DESCRIPTION_MAX + 1),
        )


# ─── List + get ──────────────────────────────────────────────────────────


async def test_list_albums_member_succeeds(env):
    await env.create_album(space_id="sp-1", owner_user_id="a-id", name="A")
    await env.create_album(space_id="sp-1", owner_user_id="a-id", name="B")
    out = await env.list_albums(space_id="sp-1", actor_user_id="b-id")
    assert len(out) == 2


async def test_list_albums_non_member_403(env):
    with pytest.raises(GalleryPermissionError):
        await env.list_albums(space_id="sp-1", actor_user_id="e-id")


async def test_get_album_unknown_404(env):
    with pytest.raises(GalleryNotFoundError):
        await env.get_album("nope", actor_user_id="a-id")


# ─── Update + delete permissions ─────────────────────────────────────────


async def test_update_album_owner_succeeds(env):
    a = await env.create_album(
        space_id="sp-1",
        owner_user_id="b-id",
        name="Original",
    )
    await env.update_album(
        a.id,
        actor_user_id="b-id",
        name="Renamed",
    )
    refreshed = await env.get_album(a.id, actor_user_id="b-id")
    assert refreshed.name == "Renamed"


async def test_update_album_space_admin_succeeds(env):
    a = await env.create_album(
        space_id="sp-1",
        owner_user_id="b-id",
        name="Bobs",
    )
    # alice = space owner → counts as admin.
    await env.update_album(
        a.id,
        actor_user_id="a-id",
        name="Admin renamed",
    )


async def test_update_album_other_user_403(env):
    a = await env.create_album(
        space_id="sp-1",
        owner_user_id="b-id",
        name="Bobs",
    )
    with pytest.raises(GalleryPermissionError):
        await env.update_album(
            a.id,
            actor_user_id="e-id",
            name="Hijack",
        )


async def test_delete_album_owner_succeeds(env):
    a = await env.create_album(
        space_id="sp-1",
        owner_user_id="b-id",
        name="X",
    )
    await env.delete_album(a.id, actor_user_id="b-id")
    with pytest.raises(GalleryNotFoundError):
        await env.get_album(a.id, actor_user_id="a-id")


async def test_delete_unknown_album_silent(env):
    # No raise.
    await env.delete_album("missing", actor_user_id="a-id")


# ─── Retention exemption ────────────────────────────────────────────────


async def test_set_retention_exempt_owner_succeeds(env):
    a = await env.create_album(
        space_id="sp-1",
        owner_user_id="a-id",
        name="Keep me",
    )
    await env.set_retention_exempt(a.id, True, actor_user_id="a-id")
    refreshed = await env.get_album(a.id, actor_user_id="a-id")
    assert refreshed.retention_exempt is True


async def test_set_retention_exempt_non_owner_403(env):
    a = await env.create_album(
        space_id="sp-1",
        owner_user_id="a-id",
        name="X",
    )
    with pytest.raises(GalleryPermissionError):
        await env.set_retention_exempt(
            a.id,
            True,
            actor_user_id="b-id",
        )


# ─── System album guards ─────────────────────────────────────────────────
#
# The auto-managed "Posts" album cannot be renamed, deleted, uploaded
# to, or have its items individually removed. Five entry points reject
# system-album mutations *before* the regular owner/admin check.


async def test_ensure_system_album_idempotent(env):
    a1 = await env.ensure_system_album(space_id=None)
    a2 = await env.ensure_system_album(space_id=None)
    assert a1.id == a2.id
    assert a1.is_system is True
    assert a1.owner_user_id is None


async def test_system_album_household_and_space_isolated(env):
    household = await env.ensure_system_album(space_id=None)
    space = await env.ensure_system_album(space_id="sp-1")
    assert household.id != space.id
    assert household.space_id is None
    assert space.space_id == "sp-1"


async def test_system_album_delete_blocked(env):
    a = await env.ensure_system_album(space_id=None)
    with pytest.raises(GalleryPermissionError):
        await env.delete_album(a.id, actor_user_id="a-id")


async def test_system_album_update_blocked(env):
    a = await env.ensure_system_album(space_id=None)
    with pytest.raises(GalleryPermissionError):
        await env.update_album(a.id, actor_user_id="a-id", name="renamed")


async def test_system_album_upload_blocked(env):
    a = await env.ensure_system_album(space_id=None)
    with pytest.raises(GalleryPermissionError):
        await env.upload_item(
            a.id,
            data=b"x" * 100,
            content_type="image/jpeg",
            caption=None,
            uploader_user_id="a-id",
        )


async def test_system_album_set_retention_exempt_blocked(env):
    a = await env.ensure_system_album(space_id=None)
    with pytest.raises(GalleryPermissionError):
        await env.set_retention_exempt(a.id, False, actor_user_id="a-id")


async def test_list_albums_pins_system_album_first(env):
    # Create a regular album first, then the system album. The list
    # must return the system album at the top regardless of created_at.
    await env.create_album(
        space_id="sp-1",
        owner_user_id="a-id",
        name="Trip",
    )
    sys = await env.ensure_system_album(space_id="sp-1")
    rows = await env.list_albums(space_id="sp-1", actor_user_id="a-id")
    assert rows[0].id == sys.id
    assert rows[0].is_system is True
