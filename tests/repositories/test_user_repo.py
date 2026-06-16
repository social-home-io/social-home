"""Tests for socialhome.repositories.user_repo."""

from __future__ import annotations

import pytest


@pytest.fixture
async def env(tmp_dir):
    """Minimal env with a user repo over a real SQLite database."""
    from socialhome.crypto import generate_identity_keypair, derive_instance_id
    from socialhome.db.database import AsyncDatabase
    from socialhome.infrastructure.event_bus import EventBus
    from socialhome.repositories.user_repo import SqliteUserRepo
    from socialhome.services.user_service import UserService

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.user_repo = SqliteUserRepo(db)
    e.user_svc = UserService(
        e.user_repo, EventBus(), own_instance_public_key=kp.public_key
    )
    yield e
    await db.shutdown()


async def test_save_and_get_by_username(env):
    """A provisioned user can be retrieved by username."""
    u = await env.user_svc.provision(username="alice", display_name="Alice")
    got = await env.user_repo.get("alice")
    assert got is not None
    assert got.user_id == u.user_id


async def test_save_persists_and_reads_back_identity_anchor(env):
    """The repo round-trips the identity_anchor column on a saved User."""
    from socialhome.domain.user import User

    await env.user_repo.save(
        User(
            user_id="uid-anchored",
            username="anchored",
            display_name="Anchored",
            identity_anchor="deadbeef" * 4,
        )
    )
    got = await env.user_repo.get("anchored")
    assert got is not None
    assert got.identity_anchor == "deadbeef" * 4
    # And it landed in the column, not just the dataclass.
    row = await env.db.fetchone(
        "SELECT identity_anchor FROM users WHERE username=?",
        ("anchored",),
    )
    assert row["identity_anchor"] == "deadbeef" * 4


async def test_set_user_identity_key_persists_both_halves(env):
    """set_user_identity_key writes the public + KEK-wrapped private columns."""
    await env.user_svc.provision(username="alice", display_name="Alice")
    await env.user_repo.set_user_identity_key(
        "alice",
        public_key_hex="ab" * 32,
        private_key_wrapped="wrapped-seed",
    )
    row = await env.db.fetchone(
        "SELECT user_identity_public_key, user_identity_private_key "
        "FROM users WHERE username=?",
        ("alice",),
    )
    assert row["user_identity_public_key"] == "ab" * 32
    assert row["user_identity_private_key"] == "wrapped-seed"


async def test_get_missing_user_returns_none(env):
    """Getting a non-existent username returns None."""
    got = await env.user_repo.get("nobody")
    assert got is None


async def test_get_user_identity_keypair_roundtrips_decrypted_seed(env):
    """get_user_identity_keypair returns the raw (public, private-seed) bytes,
    decrypting the KEK-wrapped private column via the attached key_manager."""
    import os

    from socialhome.crypto import generate_identity_keypair
    from socialhome.infrastructure.key_manager import KeyManager

    km = KeyManager(os.urandom(32))
    env.user_repo.attach_key_manager(km)
    await env.user_svc.provision(username="alice", display_name="Alice")

    kp = generate_identity_keypair()
    await env.user_repo.set_user_identity_key(
        "alice",
        public_key_hex=kp.public_key.hex(),
        private_key_wrapped=km.encrypt(kp.private_key),
    )

    got = await env.user_repo.get_user_identity_keypair("alice")
    assert got is not None
    public_key, private_seed = got
    assert public_key == kp.public_key
    assert private_seed == kp.private_key


async def test_get_user_identity_keypair_none_when_unminted(env):
    """A user with no identity columns (NULL) returns None — never a partial
    or crashing result."""
    import os

    from socialhome.infrastructure.key_manager import KeyManager

    env.user_repo.attach_key_manager(KeyManager(os.urandom(32)))
    # Provision WITHOUT a key_manager on the service so no key is minted.
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name, state) VALUES(?,?,?,?)",
        ("bob", "u-bob", "Bob", "active"),
    )
    assert await env.user_repo.get_user_identity_keypair("bob") is None
    assert await env.user_repo.get_user_identity_keypair("nobody") is None


async def test_get_user_identity_keypair_requires_key_manager(env):
    """Without an attached KEK the getter raises — never returns a wrapped
    blob masquerading as a seed."""
    await env.user_svc.provision(username="alice", display_name="Alice")
    await env.user_repo.set_user_identity_key(
        "alice",
        public_key_hex="ab" * 32,
        private_key_wrapped="wrapped",
    )
    with pytest.raises(RuntimeError):
        await env.user_repo.get_user_identity_keypair("alice")


async def test_list_active_users(env):
    """list_active returns all users with active state."""
    await env.user_svc.provision(username="alice", display_name="Alice")
    await env.user_svc.provision(username="bob", display_name="Bob")
    users = await env.user_svc.list_active()
    assert len(users) == 2


async def test_get_by_user_id(env):
    """get_by_user_id returns the user for a known user_id."""
    u = await env.user_svc.provision(username="alice", display_name="Alice")
    got = await env.user_svc.get_by_user_id(u.user_id)
    assert got.username == "alice"


async def test_list_blocked_returns_newest_first(env):
    """list_blocked returns (blocked_user_id, blocked_at) ordered desc."""
    a = await env.user_svc.provision(username="alice", display_name="Alice")
    b = await env.user_svc.provision(username="bob", display_name="Bob")
    c = await env.user_svc.provision(username="carol", display_name="Carol")

    # Force differentiable timestamps so the ORDER BY is meaningful.
    await env.db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id, blocked_at) "
        "VALUES(?, ?, ?)",
        (a.user_id, b.user_id, "2026-01-01T00:00:00Z"),
    )
    await env.db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id, blocked_at) "
        "VALUES(?, ?, ?)",
        (a.user_id, c.user_id, "2026-02-01T00:00:00Z"),
    )

    blocked = await env.user_repo.list_blocked(a.user_id)
    assert [bid for bid, _ in blocked] == [c.user_id, b.user_id]
    assert blocked[0][1] == "2026-02-01T00:00:00Z"


async def test_list_blocked_empty(env):
    """list_blocked is [] when nobody is blocked."""
    a = await env.user_svc.provision(username="alice", display_name="Alice")
    assert await env.user_repo.list_blocked(a.user_id) == []


# ── Follows (§Momentum) ──────────────────────────────────────────────


async def test_follow_unfollow_roundtrip(env):
    a = await env.user_svc.provision(username="alice", display_name="Alice")
    b = await env.user_svc.provision(username="bob", display_name="Bob")
    assert not await env.user_repo.is_following(a.user_id, b.user_id)
    await env.user_repo.follow(a.user_id, b.user_id)
    assert await env.user_repo.is_following(a.user_id, b.user_id)
    await env.user_repo.unfollow(a.user_id, b.user_id)
    assert not await env.user_repo.is_following(a.user_id, b.user_id)


async def test_follow_self_rejected(env):
    a = await env.user_svc.provision(username="alice", display_name="Alice")
    import pytest as _pt

    with _pt.raises(ValueError):
        await env.user_repo.follow(a.user_id, a.user_id)


async def test_follow_is_idempotent(env):
    a = await env.user_svc.provision(username="alice", display_name="Alice")
    b = await env.user_svc.provision(username="bob", display_name="Bob")
    await env.user_repo.follow(a.user_id, b.user_id)
    await env.user_repo.follow(a.user_id, b.user_id)  # no-op
    rows = await env.user_repo.list_following(a.user_id)
    assert len(rows) == 1


async def test_list_following_newest_first(env):
    a = await env.user_svc.provision(username="alice", display_name="Alice")
    b = await env.user_svc.provision(username="bob", display_name="Bob")
    c = await env.user_svc.provision(username="carol", display_name="Carol")
    await env.db.enqueue(
        "INSERT INTO user_follows(follower_user_id, followed_user_id, created_at) "
        "VALUES(?, ?, ?)",
        (a.user_id, b.user_id, "2026-01-01T00:00:00Z"),
    )
    await env.db.enqueue(
        "INSERT INTO user_follows(follower_user_id, followed_user_id, created_at) "
        "VALUES(?, ?, ?)",
        (a.user_id, c.user_id, "2026-02-01T00:00:00Z"),
    )
    following = await env.user_repo.list_following(a.user_id)
    assert [uid for uid, _ in following] == [c.user_id, b.user_id]


async def test_get_remote_by_member_roundtrip(env):
    """``RemoteConversationMember`` carries ``(instance_id, remote_username)``
    rather than the global ``user_id``, so the DM list / members endpoints
    need a side index on ``remote_users`` to enrich a roster row with the
    peer's display_name + picture hash."""
    from socialhome.domain.user import RemoteUser

    # ``remote_users.instance_id`` has a FK to ``remote_instances``; seed
    # the parent row first so the upsert doesn't rollback.
    await env.db.enqueue(
        """INSERT INTO remote_instances(
               id, display_name, remote_identity_pk, key_self_to_remote,
               key_remote_to_self, remote_inbox_url, local_inbox_id,
               status, source
           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "peer-b",
            "Peer B",
            "00" * 32,
            "k1",
            "k2",
            "https://peer-b.example/federation/inbox/x",
            "local-inbox",
            "confirmed",
            "manual",
        ),
    )
    await env.user_repo.upsert_remote(
        RemoteUser(
            user_id="uid-brother-remote",
            instance_id="peer-b",
            remote_username="brother",
            display_name="Brother",
            picture_hash="pic-hash-abc",
        ),
    )
    got = await env.user_repo.get_remote_by_member("peer-b", "brother")
    assert got is not None
    assert got.user_id == "uid-brother-remote"
    assert got.display_name == "Brother"
    assert got.picture_hash == "pic-hash-abc"
    # Lookup with the wrong instance OR wrong username returns None —
    # the index is on the composite, not either half alone.
    assert await env.user_repo.get_remote_by_member("peer-x", "brother") is None
    assert await env.user_repo.get_remote_by_member("peer-b", "sister") is None


async def test_set_remote_user_identity_key_persists_verified_key(env):
    """``set_remote_user_identity_key`` stores the verified per-user identity
    public key on the ``remote_users`` row without disturbing the rest of it."""
    from socialhome.domain.user import RemoteUser

    await env.db.enqueue(
        """INSERT INTO remote_instances(
               id, display_name, remote_identity_pk, key_self_to_remote,
               key_remote_to_self, remote_inbox_url, local_inbox_id,
               status, source
           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "peer-c",
            "Peer C",
            "11" * 32,
            "k1",
            "k2",
            "https://peer-c.example/federation/inbox/x",
            "local-inbox",
            "confirmed",
            "manual",
        ),
    )
    await env.user_repo.upsert_remote(
        RemoteUser(
            user_id="uid-remote-c",
            instance_id="peer-c",
            remote_username="carol",
            display_name="Carol",
        ),
    )

    await env.user_repo.set_remote_user_identity_key(
        "uid-remote-c", public_key_hex="cd" * 32
    )

    row = await env.db.fetchone(
        "SELECT user_identity_public_key, display_name FROM remote_users "
        "WHERE user_id=?",
        ("uid-remote-c",),
    )
    assert row["user_identity_public_key"] == "cd" * 32
    # The rest of the row is untouched.
    assert row["display_name"] == "Carol"
    # No anchor supplied → the column stays NULL.
    row2 = await env.db.fetchone(
        "SELECT identity_anchor FROM remote_users WHERE user_id=?",
        ("uid-remote-c",),
    )
    assert row2["identity_anchor"] is None


async def test_set_remote_user_identity_key_persists_anchor(env):
    """``set_remote_user_identity_key`` with an ``identity_anchor`` writes both
    the pubkey and the anchor column (proto v_26)."""
    from socialhome.domain.user import RemoteUser

    await env.db.enqueue(
        """INSERT INTO remote_instances(
               id, display_name, remote_identity_pk, key_self_to_remote,
               key_remote_to_self, remote_inbox_url, local_inbox_id,
               status, source
           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "peer-d",
            "Peer D",
            "22" * 32,
            "k1",
            "k2",
            "https://peer-d.example/federation/inbox/x",
            "local-inbox",
            "confirmed",
            "manual",
        ),
    )
    await env.user_repo.upsert_remote(
        RemoteUser(
            user_id="uid-remote-d",
            instance_id="peer-d",
            remote_username="dave",
            display_name="Dave",
        ),
    )

    anchor = "11111111-2222-3333-4444-555555555555"
    await env.user_repo.set_remote_user_identity_key(
        "uid-remote-d", public_key_hex="ef" * 32, identity_anchor=anchor
    )
    row = await env.db.fetchone(
        "SELECT user_identity_public_key, identity_anchor FROM remote_users "
        "WHERE user_id=?",
        ("uid-remote-d",),
    )
    assert row["user_identity_public_key"] == "ef" * 32
    assert row["identity_anchor"] == anchor

    # A later anchor-less call (a v_25 re-publish) must NOT clobber the anchor.
    await env.user_repo.set_remote_user_identity_key(
        "uid-remote-d", public_key_hex="ab" * 32
    )
    row2 = await env.db.fetchone(
        "SELECT user_identity_public_key, identity_anchor FROM remote_users "
        "WHERE user_id=?",
        ("uid-remote-d",),
    )
    assert row2["user_identity_public_key"] == "ab" * 32
    assert row2["identity_anchor"] == anchor


async def test_get_user_identity_anchor_reads_column(env):
    """``get_user_identity_anchor`` returns the local user's anchor (uuid)."""
    from socialhome.domain.user import User

    await env.user_repo.save(
        User(
            user_id="uid-a",
            username="alice",
            display_name="Alice",
            identity_anchor="anchor-uuid-1",
        )
    )
    assert await env.user_repo.get_user_identity_anchor("alice") == "anchor-uuid-1"


async def test_get_user_identity_anchor_none_when_unknown_or_null(env):
    """Unknown username → None; a row with a NULL anchor → None."""
    assert await env.user_repo.get_user_identity_anchor("nobody") is None
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name, state, identity_anchor) "
        "VALUES(?,?,?,?,?)",
        ("bob", "u-bob", "Bob", "active", None),
    )
    assert await env.user_repo.get_user_identity_anchor("bob") is None


async def test_rename_username_cascades_and_updates_post_comments(env):
    """``rename_username`` renames the users row, lets the FK cascade carry
    child rows (presence), updates the non-FK ``post_comments.author``, and
    renames the ``platform_users`` standalone-login row."""
    u = await env.user_svc.provision(username="bob", display_name="Bob")
    # FK child that should cascade via ON UPDATE CASCADE.
    await env.db.enqueue(
        "INSERT INTO presence(username, entity_id, state) VALUES(?,?,?)",
        ("bob", "person.bob", "home"),
    )
    # Standalone-login row (cascades platform_tokens, but renamed directly).
    await env.db.enqueue(
        "INSERT INTO platform_users(username, display_name) VALUES(?,?)",
        ("bob", "Bob"),
    )
    # Non-FK comment author (plain username text column).
    await env.db.enqueue(
        "INSERT INTO feed_posts(id, author, type, content) VALUES(?,?,?,?)",
        ("post-1", u.user_id, "text", "hi"),
    )
    await env.db.enqueue(
        "INSERT INTO post_comments(id, post_id, author, type, content) "
        "VALUES(?,?,?,?,?)",
        ("c-1", "post-1", "bob", "text", "nice"),
    )

    await env.user_repo.rename_username("bob", "bobby")

    assert await env.user_repo.get("bob") is None
    renamed = await env.user_repo.get("bobby")
    assert renamed is not None
    assert renamed.user_id == u.user_id  # user_id unchanged

    pres = await env.db.fetchone(
        "SELECT username FROM presence WHERE entity_id=?", ("person.bob",)
    )
    assert pres["username"] == "bobby"

    pu = await env.db.fetchone(
        "SELECT username FROM platform_users WHERE display_name=?", ("Bob",)
    )
    assert pu["username"] == "bobby"

    com = await env.db.fetchone("SELECT author FROM post_comments WHERE id=?", ("c-1",))
    assert com["author"] == "bobby"


async def test_rename_username_noop_when_no_platform_user_row(env):
    """A user with no ``platform_users`` row (HA-style) renames cleanly."""
    await env.user_svc.provision(username="ada", display_name="Ada")
    await env.user_repo.rename_username("ada", "ada2")
    assert await env.user_repo.get("ada") is None
    assert await env.user_repo.get("ada2") is not None
