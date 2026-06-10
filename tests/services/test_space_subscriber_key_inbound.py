"""Tests for :class:`SpaceSubscriberKeyInbound` (Phase 5b-b consumer).

A GFS subscriber receives the relayed ``space_subscriber_key_handoff``,
unseals the per-space content key with its key-wrap private key, verifies
the space-authority signature against its locally-mirrored space pubkey,
and imports the key — after which it can decrypt the Phase-5a relay it
previously couldn't.

Security invariants under test:

* ``target_instance_id`` ≠ us → dropped (the GFS fans the handoff to ALL
  subscribers; only the target may import);
* a forged authority signature → dropped, NO import (the relay/GFS is never
  trusted — re-verify against the local pinned pubkey);
* sealed to a DIFFERENT key-wrap key (we can't open) → InvalidTag → dropped
  gracefully;
* idempotent double-delivery → exactly one import.
"""

from __future__ import annotations

import base64
import json

import pytest

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
    sign_authority_event,
    strip_authority_sig_fields,
)
from socialhome.crypto import (
    generate_space_keypair,
    generate_x25519_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.federation.keywrap_seal import seal_to_keywrap
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_key_repo import SqliteSpaceKeyRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.space_crypto_service import (
    KEY_SUITE_AESGCM_256,
    SpaceContentEncryption,
)
from socialhome.services.space_subscriber_key_inbound import (
    SpaceSubscriberKeyInbound,
)


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    own_iid = "subscriber.home"
    kek = KeyManager.from_data_dir(tmp_dir)
    space_repo = SqliteSpaceRepo(db, key_manager=kek)
    key_repo = SqliteSpaceKeyRepo(db)
    crypto = SpaceContentEncryption(key_repo, kek, own_instance_id=own_iid)
    # Our key-wrap keypair (this subscriber's static recipient key).
    kw_kp = generate_x25519_keypair()
    # The space's authority keypair (the host signs handoffs with the seed; we
    # mirror only the public half locally and verify against it).
    skp = generate_space_keypair()

    async def _mirror_space(space_id, *, pubkey_hex):
        # A subscriber mirrors the space row WITHOUT the seed (it never holds
        # the host's private key).
        await space_repo.save(
            Space(
                id=space_id,
                name="S",
                owner_instance_id="host.home",
                owner_username="hostuser",
                identity_public_key=pubkey_hex,
                config_sequence=0,
                features=SpaceFeatures(),
                space_type=SpaceType.PUBLIC,
                join_mode=JoinMode.OPEN,
            )
        )

    svc = SpaceSubscriberKeyInbound(
        space_repo=space_repo,
        space_crypto=crypto,
    )
    svc.attach_identity(
        own_instance_id=own_iid,
        keywrap_private_key=kw_kp.private_key,
    )
    return {
        "db": db,
        "crypto": crypto,
        "space_repo": space_repo,
        "mirror_space": _mirror_space,
        "svc": svc,
        "own_iid": own_iid,
        "kw_kp": kw_kp,
        "skp": skp,
    }


def _content_key_meta(epoch, raw_key):
    return {
        "space_content_key": {
            "epoch": epoch,
            "key_suite": KEY_SUITE_AESGCM_256,
            "key_base64": base64.b64encode(raw_key).decode("ascii"),
            "rotated_by": "host.home",
        }
    }


def _handoff_frame(space_id, *, target, sealed, space_seed):
    envelope = {
        "space_id": space_id,
        "target_instance_id": target,
        "sealed": sealed,
    }
    envelope.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
            space_id=space_id,
            payload=strip_authority_sig_fields(envelope),
            space_seed=space_seed,
        )
    )
    return {
        "type": "relay",
        "event_type": AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
        "space_id": space_id,
        "from_instance": "host.home",
        "payload": envelope,
    }


async def test_valid_handoff_imports_key_and_enables_decrypt(env):
    space_id = "sp-in"
    await env["mirror_space"](space_id, pubkey_hex=env["skp"].public_key.hex())
    # The 32-byte content key the host minted (raw — what export_current_key
    # would yield on the host side). Build a Phase-5a-style ciphertext under
    # it so we can prove decrypt works AFTER the import.
    raw_key = bytes(range(32))
    epoch = 0
    meta = _content_key_meta(epoch, raw_key)
    sealed = seal_to_keywrap(
        recipient_keywrap_pub=env["kw_kp"].public_key,
        plaintext=json.dumps(meta).encode("utf-8"),
    )
    frame = _handoff_frame(
        space_id,
        target=env["own_iid"],
        sealed=sealed,
        space_seed=env["skp"].private_key,
    )

    # Before import: no key, decrypt impossible.
    assert await env["crypto"].export_current_key(space_id) is None

    await env["svc"].handle(frame)

    # After import: the key landed at the stated epoch.
    got = await env["crypto"].export_current_key(space_id)
    assert got is not None
    assert got[0] == epoch
    assert got[1] == raw_key
    # And a ciphertext encrypted under the same key now decrypts.
    _e, ct = await env["crypto"].encrypt(space_id, b"phase-5a-relayed-post")
    pt = await env["crypto"].decrypt(space_id, epoch, ct)
    assert pt == b"phase-5a-relayed-post"


async def test_not_target_dropped(env):
    space_id = "sp-nottarget"
    await env["mirror_space"](space_id, pubkey_hex=env["skp"].public_key.hex())
    raw_key = bytes(range(32))
    meta = _content_key_meta(0, raw_key)
    sealed = seal_to_keywrap(
        recipient_keywrap_pub=env["kw_kp"].public_key,
        plaintext=json.dumps(meta).encode("utf-8"),
    )
    frame = _handoff_frame(
        space_id,
        target="someone-else.home",  # not us
        sealed=sealed,
        space_seed=env["skp"].private_key,
    )

    await env["svc"].handle(frame)

    assert await env["crypto"].export_current_key(space_id) is None


async def test_forged_authority_sig_dropped(env):
    """A handoff signed by a DIFFERENT (attacker) seed must not import — the
    receiver re-verifies the authority sig against the locally-pinned space
    pubkey and never trusts the relay/GFS."""
    space_id = "sp-forged"
    await env["mirror_space"](space_id, pubkey_hex=env["skp"].public_key.hex())
    raw_key = bytes(range(32))
    meta = _content_key_meta(0, raw_key)
    sealed = seal_to_keywrap(
        recipient_keywrap_pub=env["kw_kp"].public_key,
        plaintext=json.dumps(meta).encode("utf-8"),
    )
    attacker = generate_space_keypair()
    frame = _handoff_frame(
        space_id,
        target=env["own_iid"],
        sealed=sealed,
        space_seed=attacker.private_key,  # NOT the pinned space seed
    )

    await env["svc"].handle(frame)

    assert await env["crypto"].export_current_key(space_id) is None


async def test_sealed_to_other_keywrap_key_dropped(env):
    """Sealed to a key-wrap pubkey we don't hold the private half for →
    InvalidTag on open → dropped gracefully, no import, no crash."""
    space_id = "sp-otherseal"
    await env["mirror_space"](space_id, pubkey_hex=env["skp"].public_key.hex())
    raw_key = bytes(range(32))
    meta = _content_key_meta(0, raw_key)
    other_kw = generate_x25519_keypair()  # not ours
    sealed = seal_to_keywrap(
        recipient_keywrap_pub=other_kw.public_key,
        plaintext=json.dumps(meta).encode("utf-8"),
    )
    frame = _handoff_frame(
        space_id,
        target=env["own_iid"],
        sealed=sealed,
        space_seed=env["skp"].private_key,
    )

    await env["svc"].handle(frame)

    assert await env["crypto"].export_current_key(space_id) is None


async def test_idempotent_double_delivery(env):
    space_id = "sp-idem"
    await env["mirror_space"](space_id, pubkey_hex=env["skp"].public_key.hex())
    raw_key = bytes(range(32))
    epoch = 0
    meta = _content_key_meta(epoch, raw_key)
    sealed = seal_to_keywrap(
        recipient_keywrap_pub=env["kw_kp"].public_key,
        plaintext=json.dumps(meta).encode("utf-8"),
    )
    frame = _handoff_frame(
        space_id,
        target=env["own_iid"],
        sealed=sealed,
        space_seed=env["skp"].private_key,
    )

    await env["svc"].handle(frame)
    await env["svc"].handle(frame)  # second delivery — idempotent

    got = await env["crypto"].export_current_key(space_id)
    assert got is not None
    assert got[1] == raw_key


async def test_unmirrored_space_dropped(env):
    """A handoff for a space we don't mirror locally → dropped (no pubkey to
    verify against)."""
    space_id = "sp-unknown"
    raw_key = bytes(range(32))
    meta = _content_key_meta(0, raw_key)
    sealed = seal_to_keywrap(
        recipient_keywrap_pub=env["kw_kp"].public_key,
        plaintext=json.dumps(meta).encode("utf-8"),
    )
    frame = _handoff_frame(
        space_id,
        target=env["own_iid"],
        sealed=sealed,
        space_seed=env["skp"].private_key,
    )

    await env["svc"].handle(frame)  # must not crash

    assert await env["crypto"].export_current_key(space_id) is None


async def test_non_handoff_frame_ignored(env):
    await env["svc"].handle(
        {"type": "relay", "event_type": "space_post_public", "payload": {}}
    )
    await env["svc"].handle({"type": "relay", "event_type": "other"})
