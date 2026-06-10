"""Tests for :class:`SpaceSubscriberKeyOutbound` (Phase 5b-b producer).

On a GFS ``new_subscriber`` frame, a seed-holding household seals the
per-space content key to the new subscriber's published key-wrap pubkey
and relays it through the content-blind GFS as a space-authority-signed
``space_subscriber_key_handoff`` envelope.

Security invariants under test:

* the wire envelope carries NO plaintext key bytes (only inside
  ``sealed.ciphertext``);
* the subscriber's key-wrap binding is VERIFIED before sealing — a forged
  binding (key-wrap sig that doesn't match the identity) → NO relay
  (anti-GFS-substitution);
* a non-seed-holder → no relay; a private/household space → no relay.
"""

from __future__ import annotations

import base64
import json

import pytest

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
    strip_authority_sig_fields,
    verify_authority_event,
)
from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_space_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.federation.keywrap_seal import open_keywrap
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_key_repo import SqliteSpaceKeyRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.space_crypto_service import SpaceContentEncryption
from socialhome.services.space_subscriber_key_outbound import (
    SpaceSubscriberKeyOutbound,
)


class _CaptureGfs:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def publish_space_event(
        self, *, space_id, event_type, payload, from_instance
    ) -> int:
        self.calls.append(
            {
                "space_id": space_id,
                "event_type": event_type,
                "payload": payload,
                "from_instance": from_instance,
            }
        )
        return 1


def _subscriber_identity():
    """Return a subscriber's (identity_kp, keywrap_kp, instance_id, keywrap_sig)."""
    id_kp = generate_identity_keypair()
    kw_kp = generate_x25519_keypair()
    instance_id = derive_instance_id(id_kp.public_key)
    keywrap_sig = b64url_encode(sign_ed25519(id_kp.private_key, kw_kp.public_key))
    return id_kp, kw_kp, instance_id, keywrap_sig


def _new_subscriber_frame(space_id, instance_id, id_pub, kw_pub, keywrap_sig):
    return {
        "type": "new_subscriber",
        "space_id": space_id,
        "subscriber": {
            "instance_id": instance_id,
            "identity_public_key": id_pub.hex(),
            "keywrap_public_key": kw_pub.hex(),
            "kem_suite": "x25519",
            "keywrap_sig": keywrap_sig,
        },
    }


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    own_iid = "alpha.home"
    kek = KeyManager.from_data_dir(tmp_dir)
    space_repo = SqliteSpaceRepo(db, key_manager=kek)
    key_repo = SqliteSpaceKeyRepo(db)
    crypto = SpaceContentEncryption(key_repo, kek, own_instance_id=own_iid)
    gfs = _CaptureGfs()

    async def _make_space(space_id, stype, *, with_seed):
        skp = generate_space_keypair()
        await space_repo.save(
            Space(
                id=space_id,
                name="S",
                owner_instance_id=own_iid,
                owner_username="alice",
                identity_public_key=skp.public_key.hex(),
                config_sequence=0,
                features=SpaceFeatures(),
                space_type=stype,
                join_mode=JoinMode.OPEN,
            )
        )
        if with_seed:
            await space_repo.set_space_seed(space_id, skp.private_key)
        await crypto.initialise_for_space(space_id)
        return skp

    svc = SpaceSubscriberKeyOutbound(
        space_repo=space_repo,
        space_crypto=crypto,
        gfs_service=gfs,
    )
    svc.attach_identity(own_instance_id=own_iid)
    return {
        "db": db,
        "gfs": gfs,
        "crypto": crypto,
        "space_repo": space_repo,
        "make_space": _make_space,
        "svc": svc,
    }


async def test_valid_binding_relays_sealed_key_handoff(env):
    skp = await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    id_kp, kw_kp, sub_iid, keywrap_sig = _subscriber_identity()
    frame = _new_subscriber_frame(
        "sp-pub", sub_iid, id_kp.public_key, kw_kp.public_key, keywrap_sig
    )

    await env["svc"].handle(frame)

    assert len(env["gfs"].calls) == 1
    call = env["gfs"].calls[0]
    assert call["event_type"] == AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF
    assert call["space_id"] == "sp-pub"
    envelope = call["payload"]
    assert envelope["space_id"] == "sp-pub"
    assert envelope["target_instance_id"] == sub_iid
    assert set(envelope) >= {
        "space_id",
        "target_instance_id",
        "sealed",
        "authority_sig",
        "authority_sig_suite",
    }

    # Authority signature verifies against the space public key (under the
    # handoff event type).
    assert verify_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
        space_id="sp-pub",
        payload=strip_authority_sig_fields(envelope),
        authority_sig=envelope["authority_sig"],
        authority_sig_suite=envelope["authority_sig_suite"],
        space_public_key=skp.public_key,
    )

    # GFS-blind: the raw content key bytes / its base64 NEVER appear on the wire
    # envelope — only inside sealed.ciphertext.
    epoch, raw_key = await env["crypto"].export_current_key("sp-pub")
    key_b64 = base64.b64encode(raw_key).decode("ascii")
    blob = json.dumps(envelope)
    assert key_b64 not in blob
    assert raw_key.hex() not in blob

    # The subscriber can open the seal with its key-wrap private key and recover
    # the content-key meta the inbound consumer feeds to
    # apply_space_content_key_from_metadata.
    pt = open_keywrap(
        sealed=envelope["sealed"], recipient_keywrap_priv=kw_kp.private_key
    )
    meta = json.loads(pt)
    inner = meta["space_content_key"]
    assert inner["epoch"] == epoch
    assert inner["key_base64"] == key_b64
    assert inner["key_suite"] == "aesgcm-256"


async def test_forged_binding_no_relay(env):
    """Anti-substitution: a key-wrap pubkey whose self-signature does NOT match
    the subscriber identity (a malicious GFS swapped in a key it controls) is
    rejected — no relay, the content key is never sealed to the attacker."""
    await env["make_space"]("sp-pub2", SpaceType.PUBLIC, with_seed=True)
    id_kp, _kw_kp, sub_iid, _good_sig = _subscriber_identity()
    # Attacker substitutes its OWN key-wrap key but cannot self-sign it as the
    # subscriber's identity, so it signs with an unrelated identity.
    attacker_kw = generate_x25519_keypair()
    attacker_id = generate_identity_keypair()
    forged_sig = b64url_encode(
        sign_ed25519(attacker_id.private_key, attacker_kw.public_key)
    )
    frame = _new_subscriber_frame(
        "sp-pub2", sub_iid, id_kp.public_key, attacker_kw.public_key, forged_sig
    )

    await env["svc"].handle(frame)

    assert env["gfs"].calls == []


async def test_non_seed_holder_no_relay(env):
    await env["make_space"]("sp-noseed", SpaceType.PUBLIC, with_seed=False)
    id_kp, kw_kp, sub_iid, keywrap_sig = _subscriber_identity()
    frame = _new_subscriber_frame(
        "sp-noseed", sub_iid, id_kp.public_key, kw_kp.public_key, keywrap_sig
    )

    await env["svc"].handle(frame)

    assert env["gfs"].calls == []


async def test_private_space_no_relay(env):
    await env["make_space"]("sp-priv", SpaceType.PRIVATE, with_seed=True)
    id_kp, kw_kp, sub_iid, keywrap_sig = _subscriber_identity()
    frame = _new_subscriber_frame(
        "sp-priv", sub_iid, id_kp.public_key, kw_kp.public_key, keywrap_sig
    )

    await env["svc"].handle(frame)

    assert env["gfs"].calls == []


async def test_household_space_no_relay(env):
    await env["make_space"]("sp-hh", SpaceType.HOUSEHOLD, with_seed=True)
    id_kp, kw_kp, sub_iid, keywrap_sig = _subscriber_identity()
    frame = _new_subscriber_frame(
        "sp-hh", sub_iid, id_kp.public_key, kw_kp.public_key, keywrap_sig
    )

    await env["svc"].handle(frame)

    assert env["gfs"].calls == []


async def test_no_keywrap_key_no_relay(env):
    """A subscriber that shipped no key-wrap key (older HFS) is skipped (can't
    be sealed-to) — no relay, no crash."""
    await env["make_space"]("sp-older", SpaceType.PUBLIC, with_seed=True)
    id_kp = generate_identity_keypair()
    sub_iid = derive_instance_id(id_kp.public_key)
    frame = {
        "type": "new_subscriber",
        "space_id": "sp-older",
        "subscriber": {
            "instance_id": sub_iid,
            "identity_public_key": id_kp.public_key.hex(),
            "keywrap_public_key": "",
            "kem_suite": "",
            "keywrap_sig": "",
        },
    }

    await env["svc"].handle(frame)

    assert env["gfs"].calls == []


async def test_malformed_frame_no_crash(env):
    await env["make_space"]("sp-mal", SpaceType.PUBLIC, with_seed=True)
    await env["svc"].handle({"type": "new_subscriber"})  # no subscriber dict
    await env["svc"].handle({"type": "new_subscriber", "space_id": "sp-mal"})
    await env["svc"].handle(
        {"type": "new_subscriber", "space_id": "unknown-space", "subscriber": {}}
    )
    assert env["gfs"].calls == []
