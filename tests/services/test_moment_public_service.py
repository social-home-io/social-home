"""Smoke tests for the public-Momentum service / repos / inbound handler.

These exercise the registration + follow round-trips and the inbound
verify path. The HTTP round-trip to the GFS is mocked at the service
edge — the real wire path lives behind the persistent WS supervisor
and gets exercised end-to-end in :file:`tests/scenarios/`.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock


from socialhome.crypto import b64url_encode, sign_ed25519
from socialhome.domain.events import MomentCreated
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.moment_public_repo import (
    SqliteMomentPublicFollowRepo,
    SqliteMomentPublicRegistrationRepo,
)
from socialhome.services.moment_public_inbound import MomentPublicInbound

# A 32-byte Ed25519 seed and the matching 32-byte public-key hex
# derived once for the inbound signature-verify tests.
_AUTHOR_SEED = b"\x11" * 32


def _author_pk_hex() -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    sk = Ed25519PrivateKey.from_private_bytes(_AUTHOR_SEED)
    return sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def _signed_envelope(payload: dict, *, seed: bytes = _AUTHOR_SEED) -> dict:
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    signed = dict(payload)
    signed["signature"] = b64url_encode(sign_ed25519(seed, canonical))
    return signed


# ── Repos ────────────────────────────────────────────────────────────────


async def test_registration_repo_upsert_then_list_then_default_share(db):
    repo = SqliteMomentPublicRegistrationRepo(db)
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u1','alice','Alice','active')"
    )
    await db.enqueue(
        "INSERT INTO gfs_connections("
        "id, gfs_instance_id, display_name, public_key, inbox_url, "
        "status, paired_at) "
        "VALUES('g1','gfs-1','GFS One','aa'*32,'https://gfs1.example','active', datetime('now'))"
    )
    reg = await repo.upsert(user_id="u1", gfs_id="g1", default_share=True)
    assert reg.user_id == "u1" and reg.gfs_id == "g1"
    rows = await repo.list_for_user("u1")
    assert len(rows) == 1 and rows[0].default_share is True
    await repo.set_default_share(user_id="u1", gfs_id="g1", default_share=False)
    after = await repo.get(user_id="u1", gfs_id="g1")
    assert after is not None and after.default_share is False


async def test_follow_repo_round_trip(db):
    repo = SqliteMomentPublicFollowRepo(db)
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u1','alice','Alice','active')"
    )
    await db.enqueue(
        "INSERT INTO gfs_connections("
        "id, gfs_instance_id, display_name, public_key, inbox_url, "
        "status, paired_at) "
        "VALUES('g1','gfs-1','GFS One','aa'*32,'https://gfs1.example','active', datetime('now'))"
    )
    follow = await repo.upsert(
        follower_user_id="u1",
        followed_user_id="u-remote",
        gfs_id="g1",
        followed_instance_pk=_author_pk_hex(),
        followed_username="bob",
        followed_display_name="Bob",
    )
    assert follow.followed_user_id == "u-remote"
    pk = await repo.lookup_followed_pk(
        follower_user_id="u1", followed_user_id="u-remote", gfs_id="g1"
    )
    assert pk == _author_pk_hex()
    rows = await repo.list_for_follower("u1")
    assert len(rows) == 1


# ── Inbound ──────────────────────────────────────────────────────────────


async def test_inbound_persists_and_publishes_on_valid_signature(db):
    bus = EventBus()
    captured: list[MomentCreated] = []
    bus.subscribe(MomentCreated, lambda e: captured.append(e))
    follow_repo = SqliteMomentPublicFollowRepo(db)
    from socialhome.repositories.moment_repo import SqliteMomentRepo

    moment_repo = SqliteMomentRepo(db)

    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u1','alice','Alice','active')"
    )
    await db.enqueue(
        "INSERT INTO gfs_connections("
        "id, gfs_instance_id, display_name, public_key, inbox_url, "
        "status, paired_at) "
        "VALUES('g1','gfs-1','GFS One','aa'*32,'https://gfs1.example','active', datetime('now'))"
    )
    await follow_repo.upsert(
        follower_user_id="u1",
        followed_user_id="u-remote",
        gfs_id="g1",
        followed_instance_pk=_author_pk_hex(),
        followed_username="bob",
        followed_display_name="Bob",
    )
    inbound = MomentPublicInbound(
        bus=bus, moment_repo=moment_repo, follow_repo=follow_repo
    )
    envelope = _signed_envelope(
        {
            "moment_id": "m-1",
            "author_user_id": "u-remote",
            "author_username": "bob",
            "author_display_name": "Bob",
            "content": "hello",
            "media_url": None,
            "media_type": None,
            "duration_ms": None,
            "parent_moment_id": None,
            "origin_instance_id": "inst-remote",
            "created_at": "2026-05-06T12:00:00Z",
            "expires_at": "2026-05-07T12:00:00Z",
        }
    )
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": envelope}, gfs_id="g1"
    )
    assert len(captured) == 1
    saved = await moment_repo.get("m-1")
    assert saved is not None
    assert saved.received_via == "gfs"
    assert saved.received_via_gfs_id == "g1"
    assert saved.is_public is True


async def test_inbound_drops_when_signature_does_not_match(db):
    bus = EventBus()
    captured: list[MomentCreated] = []
    bus.subscribe(MomentCreated, lambda e: captured.append(e))
    follow_repo = SqliteMomentPublicFollowRepo(db)
    from socialhome.repositories.moment_repo import SqliteMomentRepo

    moment_repo = SqliteMomentRepo(db)

    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u1','alice','Alice','active')"
    )
    await db.enqueue(
        "INSERT INTO gfs_connections("
        "id, gfs_instance_id, display_name, public_key, inbox_url, "
        "status, paired_at) "
        "VALUES('g1','gfs-1','GFS One','aa'*32,'https://gfs1.example','active', datetime('now'))"
    )
    # Cache the *real* author pk; sign the envelope with a different seed
    # so verification must fail.
    await follow_repo.upsert(
        follower_user_id="u1",
        followed_user_id="u-remote",
        gfs_id="g1",
        followed_instance_pk=_author_pk_hex(),
        followed_username="bob",
        followed_display_name="Bob",
    )
    inbound = MomentPublicInbound(
        bus=bus, moment_repo=moment_repo, follow_repo=follow_repo
    )
    forged = _signed_envelope(
        {
            "moment_id": "m-2",
            "author_user_id": "u-remote",
            "content": "forged",
            "media_url": None,
            "media_type": None,
            "duration_ms": None,
            "parent_moment_id": None,
            "origin_instance_id": "inst-remote",
            "created_at": "2026-05-06T12:00:00Z",
            "expires_at": "2026-05-07T12:00:00Z",
        },
        seed=b"\x42" * 32,  # different signing seed → bad signature
    )
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": forged}, gfs_id="g1"
    )
    assert captured == []
    assert await moment_repo.get("m-2") is None


# ── Outbound (skip — needs the full app fixture; see scenarios) ──────────
# Smoke-only: verify wire() subscribes without exploding.


async def test_outbound_wire_subscribes_to_bus():
    from socialhome.services.moment_public_outbound import MomentPublicOutbound

    bus = EventBus()
    sub = MomentPublicOutbound(
        bus=bus,
        moment_repo=MagicMock(),
        registration_repo=MagicMock(),
        user_repo=MagicMock(),
        gfs_repo=MagicMock(),
    )
    sub.wire()  # should not raise; subscribers are just stored
