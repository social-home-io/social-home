"""§27.9: GFS-relayed events use sealed-sender encryption.

The Global Federation Server can read only routing fields
(``space_id``, ``epoch``); sender identity + payload are encrypted
under the per-epoch space content key, and the sender authenticates
itself to the recipient with an Ed25519 ``outer_signature`` (so a
key-holder can't forge content as another member and a GFS can't
substitute a sealed blob undetected).

Verifies the sealed-sender envelope shape, decryption, and
sender-authentication invariants.
"""

from __future__ import annotations

import json
import os

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.federation.sealed_sender import (
    SealedSenderAuthError,
    seal_envelope,
    unseal_envelope,
)


pytestmark = pytest.mark.security


# ─── Test helpers ────────────────────────────────────────────────────────


def _sender():
    kp = generate_identity_keypair()
    return kp.private_key, kp.public_key, derive_instance_id(kp.public_key)


def _lookup(iid_to_pub):
    return iid_to_pub.get


# ─── Sealed envelope visible-field allowlist ────────────────────────────

_ALLOWED_GFS_VISIBLE_FIELDS: frozenset[str] = frozenset(
    {
        "sealed",
        "space_id",
        "epoch",
        "encrypted_sender",
        "encrypted_payload",
        # PQ-forward suite identifier — see ``aead_suite`` on
        # ``SealedEnvelope``. The receiver needs it in the clear to
        # pick the right AEAD primitive (same status as ``epoch``,
        # which selects the key); the GFS doesn't gain payload
        # content from it. Adding it does NOT relax the
        # minimization invariant — it's metadata about the cipher,
        # not metadata about the message.
        "aead_suite",
        # Ed25519 signature by the sender's identity key over the
        # routing fields + ciphertexts. It authenticates the sealed
        # sender to the recipient (no forgery / no GFS substitution).
        # It's a signature over ciphertext — it leaks no plaintext
        # sender id or payload content, so it doesn't relax
        # minimization (the tests below assert nothing sensitive
        # appears on the wire even with it present).
        "outer_signature",
    }
)


def test_sealed_envelope_only_exposes_routing():
    key = os.urandom(32)
    seed, _pub, _iid = _sender()
    env = seal_envelope(
        space_id="sp-1",
        epoch=3,
        sender_instance_id="alice-inst",
        payload_json='{"content":"secret"}',
        space_content_key=key,
        signer_seed=seed,
    )
    d = env.to_dict()
    extras = set(d.keys()) - _ALLOWED_GFS_VISIBLE_FIELDS
    assert extras == set(), f"Sealed envelope leaks unexpected fields: {sorted(extras)}"


def test_sender_id_never_appears_on_wire():
    key = os.urandom(32)
    seed, _pub, _iid = _sender()
    distinctive = "instance-alpha-very-distinctive-name"
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id=distinctive,
        payload_json="{}",
        space_content_key=key,
        signer_seed=seed,
    )
    wire = json.dumps(env.to_dict())
    assert distinctive not in wire


def test_payload_content_never_appears_on_wire():
    key = os.urandom(32)
    seed, _pub, _iid = _sender()
    secret = "auction-reserve-price-USD-9999"
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id="x",
        payload_json=f'{{"reserve":"{secret}"}}',
        space_content_key=key,
        signer_seed=seed,
    )
    wire = json.dumps(env.to_dict())
    assert secret not in wire


def test_routing_fields_remain_in_clear():
    """``space_id`` + ``epoch`` MUST be plaintext for GFS routing."""
    key = os.urandom(32)
    seed, _pub, _iid = _sender()
    env = seal_envelope(
        space_id="sp-public-routing-id",
        epoch=42,
        sender_instance_id="x",
        payload_json="{}",
        space_content_key=key,
        signer_seed=seed,
    )
    d = env.to_dict()
    assert d["space_id"] == "sp-public-routing-id"
    assert d["epoch"] == 42


def test_unseal_with_wrong_key_fails():
    key1 = os.urandom(32)
    key2 = os.urandom(32)
    seed, pub, iid = _sender()
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id=iid,
        payload_json="{}",
        space_content_key=key1,
        signer_seed=seed,
    )
    with pytest.raises(Exception):
        unseal_envelope(
            env,
            space_content_key=key2,
            sender_pk_lookup=_lookup({iid: pub}),
        )


def test_recipient_decrypts_to_original_payload():
    """Round-trip: GFS forwards the bytes; recipient decrypts + authenticates."""
    key = os.urandom(32)
    seed, pub, iid = _sender()
    env = seal_envelope(
        space_id="sp-1",
        epoch=5,
        sender_instance_id=iid,
        payload_json='{"content":"hello"}',
        space_content_key=key,
        signer_seed=seed,
    )
    out = unseal_envelope(
        env,
        space_content_key=key,
        sender_pk_lookup=_lookup({iid: pub}),
    )
    assert out.sender_instance_id == iid
    assert out.payload == {"content": "hello"}


def test_keyholder_cannot_forge_sender():
    """§27.9 security: a key-holder signing with the WRONG identity seed
    cannot impersonate another member — the recipient verifies the
    outer_signature against the claimed sender's registered pubkey."""
    key = os.urandom(32)
    attacker_seed, _attacker_pub, _attacker_iid = _sender()
    _victim_seed, victim_pub, victim_iid = _sender()
    forged = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id=victim_iid,
        payload_json='{"content":"forged"}',
        space_content_key=key,
        signer_seed=attacker_seed,
    )
    with pytest.raises(SealedSenderAuthError):
        unseal_envelope(
            forged,
            space_content_key=key,
            sender_pk_lookup=_lookup({victim_iid: victim_pub}),
        )
