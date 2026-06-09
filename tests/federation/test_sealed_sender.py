"""Tests for sealed-sender envelope encryption (§4.3)."""

from __future__ import annotations

import os

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.federation.sealed_sender import (
    AEAD_SUITE_AESGCM_256,
    SealedEnvelope,
    SealedSenderAuthError,
    UnsupportedAeadSuite,
    seal_envelope,
    unseal_envelope,
)


# ─── Test helpers ────────────────────────────────────────────────────────


def _make_sender():
    """Return ``(seed, pub, instance_id)`` for a freshly minted identity."""
    kp = generate_identity_keypair()
    return kp.private_key, kp.public_key, derive_instance_id(kp.public_key)


def _lookup_for(*pairs):
    """Build a sender_pk_lookup mapping instance_id → pubkey bytes."""
    table = {iid: pub for iid, pub in pairs}
    return lambda iid: table.get(iid)


# ─── seal / unseal roundtrip ─────────────────────────────────────────────


def test_seal_unseal_roundtrip():
    key = os.urandom(32)
    seed, pub, iid = _make_sender()
    env = seal_envelope(
        space_id="sp-1",
        epoch=3,
        sender_instance_id=iid,
        payload_json='{"text": "hello space"}',
        space_content_key=key,
        signer_seed=seed,
    )
    out = unseal_envelope(
        env,
        space_content_key=key,
        sender_pk_lookup=_lookup_for((iid, pub)),
    )
    assert out.sender_instance_id == iid
    assert out.payload == {"text": "hello space"}


def test_to_dict_roundtrips_via_from_dict():
    key = os.urandom(32)
    seed, _pub, iid = _make_sender()
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id=iid,
        payload_json="{}",
        space_content_key=key,
        signer_seed=seed,
    )
    d = env.to_dict()
    assert d["sealed"] is True
    assert d["outer_signature"]
    assert SealedEnvelope.from_dict(d) == env


# ─── Privacy invariants (the whole point) ────────────────────────────────


def test_sender_id_not_present_in_wire_format():
    """A GFS that only sees the wire format must not be able to read sender."""
    key = os.urandom(32)
    seed, _pub, iid = _make_sender()
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id=iid,
        payload_json="{}",
        space_content_key=key,
        signer_seed=seed,
    )
    wire_str = repr(env.to_dict())
    assert iid not in wire_str
    # Encrypted blob is base64url + ":", obviously contains no plaintext.
    assert iid not in env.encrypted_sender


def test_payload_text_not_present_in_wire():
    key = os.urandom(32)
    seed, _pub, _iid = _make_sender()
    secret = "super-secret-message-content"
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id="x",
        payload_json=f'{{"content": "{secret}"}}',
        space_content_key=key,
        signer_seed=seed,
    )
    assert secret not in env.encrypted_payload
    assert secret not in repr(env.to_dict())


def test_routing_fields_remain_plaintext():
    """space_id + epoch must be plaintext for GFS routing."""
    key = os.urandom(32)
    seed, _pub, _iid = _make_sender()
    env = seal_envelope(
        space_id="sp-public-routing-ok",
        epoch=42,
        sender_instance_id="x",
        payload_json="{}",
        space_content_key=key,
        signer_seed=seed,
    )
    d = env.to_dict()
    assert d["space_id"] == "sp-public-routing-ok"
    assert d["epoch"] == 42


# ─── Outer-signature authenticity (the core security regression) ─────────


def test_forged_sender_rejected():
    """A key-holder (has the shared space content key) seals content
    *claiming* to be victim V, but signs with the attacker's OWN seed.
    The recipient looks up V's real pubkey and the signature MUST NOT
    verify → unseal raises. Without the outer signature, ANY key-holder
    could forge content as any member."""
    key = os.urandom(32)
    attacker_seed, _attacker_pub, _attacker_iid = _make_sender()
    _victim_seed, victim_pub, victim_iid = _make_sender()

    # Attacker forges an envelope: sender_instance_id = victim,
    # but the outer signature is produced with the attacker's seed.
    forged = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id=victim_iid,
        payload_json='{"text": "I am the victim"}',
        space_content_key=key,
        signer_seed=attacker_seed,
    )

    # Recipient resolves the claimed sender to the victim's real pubkey.
    with pytest.raises(SealedSenderAuthError):
        unseal_envelope(
            forged,
            space_content_key=key,
            sender_pk_lookup=_lookup_for((victim_iid, victim_pub)),
        )


def test_tampered_payload_ct_fails_verify():
    """Flipping encrypted_payload after sealing breaks the outer sig."""
    key = os.urandom(32)
    seed, pub, iid = _make_sender()
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id=iid,
        payload_json="{}",
        space_content_key=key,
        signer_seed=seed,
    )
    last_two = env.encrypted_payload[-2:]
    replacement = "BB" if last_two != "BB" else "CC"
    bad = SealedEnvelope(
        space_id=env.space_id,
        epoch=env.epoch,
        encrypted_sender=env.encrypted_sender,
        encrypted_payload=env.encrypted_payload[:-2] + replacement,
        outer_signature=env.outer_signature,
    )
    with pytest.raises(Exception):
        unseal_envelope(
            bad,
            space_content_key=key,
            sender_pk_lookup=_lookup_for((iid, pub)),
        )


def test_tampered_sender_ct_fails():
    key = os.urandom(32)
    seed, pub, iid = _make_sender()
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id=iid,
        payload_json="{}",
        space_content_key=key,
        signer_seed=seed,
    )
    last_two = env.encrypted_sender[-2:]
    replacement = "BB" if last_two != "BB" else "CC"
    bad = SealedEnvelope(
        space_id=env.space_id,
        epoch=env.epoch,
        encrypted_sender=env.encrypted_sender[:-2] + replacement,
        encrypted_payload=env.encrypted_payload,
        outer_signature=env.outer_signature,
    )
    with pytest.raises(Exception):
        unseal_envelope(
            bad,
            space_content_key=key,
            sender_pk_lookup=_lookup_for((iid, pub)),
        )


def test_tampered_space_id_fails():
    """The outer signature covers space_id — a GFS substituting it is
    detected even before the AEAD AAD check."""
    key = os.urandom(32)
    seed, pub, iid = _make_sender()
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id=iid,
        payload_json="{}",
        space_content_key=key,
        signer_seed=seed,
    )
    altered = SealedEnvelope(
        space_id="sp-DIFFERENT",
        epoch=env.epoch,
        encrypted_sender=env.encrypted_sender,
        encrypted_payload=env.encrypted_payload,
        outer_signature=env.outer_signature,
    )
    with pytest.raises(Exception):
        unseal_envelope(
            altered,
            space_content_key=key,
            sender_pk_lookup=_lookup_for((iid, pub)),
        )


def test_tampered_epoch_fails():
    key = os.urandom(32)
    seed, pub, iid = _make_sender()
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id=iid,
        payload_json="{}",
        space_content_key=key,
        signer_seed=seed,
    )
    altered = SealedEnvelope(
        space_id=env.space_id,
        epoch=99,
        encrypted_sender=env.encrypted_sender,
        encrypted_payload=env.encrypted_payload,
        outer_signature=env.outer_signature,
    )
    with pytest.raises(Exception):
        unseal_envelope(
            altered,
            space_content_key=key,
            sender_pk_lookup=_lookup_for((iid, pub)),
        )


def test_unknown_sender_rejected():
    """A sender_instance_id the recipient can't resolve to a pubkey
    cannot be authenticated → fail closed."""
    key = os.urandom(32)
    seed, _pub, iid = _make_sender()
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id=iid,
        payload_json="{}",
        space_content_key=key,
        signer_seed=seed,
    )
    with pytest.raises(SealedSenderAuthError):
        unseal_envelope(
            env,
            space_content_key=key,
            sender_pk_lookup=lambda _iid: None,
        )


def test_pubkey_not_matching_claimed_id_rejected():
    """A returned pubkey that doesn't hash to the claimed
    sender_instance_id is rejected (binds key to id — mirrors #596)."""
    key = os.urandom(32)
    seed, _pub, iid = _make_sender()
    _other_seed, other_pub, _other_iid = _make_sender()
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id=iid,
        payload_json="{}",
        space_content_key=key,
        signer_seed=seed,
    )
    # Lookup returns a pubkey that does NOT derive to ``iid``.
    with pytest.raises(SealedSenderAuthError):
        unseal_envelope(
            env,
            space_content_key=key,
            sender_pk_lookup=lambda _iid: other_pub,
        )


def test_malformed_outer_signature_b64_rejected():
    key = os.urandom(32)
    seed, pub, iid = _make_sender()
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id=iid,
        payload_json="{}",
        space_content_key=key,
        signer_seed=seed,
    )
    bad = SealedEnvelope(
        space_id=env.space_id,
        epoch=env.epoch,
        encrypted_sender=env.encrypted_sender,
        encrypted_payload=env.encrypted_payload,
        outer_signature="not-valid-base64-!@#",
    )
    with pytest.raises(SealedSenderAuthError):
        unseal_envelope(
            bad,
            space_content_key=key,
            sender_pk_lookup=_lookup_for((iid, pub)),
        )


# ─── AEAD-level tampering (unchanged paths) ──────────────────────────────


def test_wrong_key_fails_decrypt():
    key1 = os.urandom(32)
    key2 = os.urandom(32)
    seed, pub, iid = _make_sender()
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
            sender_pk_lookup=_lookup_for((iid, pub)),
        )


# ─── Validation guards ──────────────────────────────────────────────────


def test_seal_rejects_wrong_key_size():
    seed, _pub, _iid = _make_sender()
    with pytest.raises(ValueError):
        seal_envelope(
            space_id="sp-1",
            epoch=0,
            sender_instance_id="x",
            payload_json="{}",
            space_content_key=b"too-short",
            signer_seed=seed,
        )


def test_unseal_rejects_wrong_key_size():
    key = os.urandom(32)
    seed, pub, iid = _make_sender()
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id=iid,
        payload_json="{}",
        space_content_key=key,
        signer_seed=seed,
    )
    with pytest.raises(ValueError):
        unseal_envelope(
            env,
            space_content_key=b"short",
            sender_pk_lookup=_lookup_for((iid, pub)),
        )


def test_from_dict_rejects_unsealed_envelope():
    with pytest.raises(ValueError):
        SealedEnvelope.from_dict({"space_id": "x"})


def test_from_dict_rejects_malformed():
    with pytest.raises(ValueError):
        SealedEnvelope.from_dict({"sealed": True})


def test_from_dict_rejects_missing_outer_signature():
    """An envelope with no outer_signature is unsigned and MUST be
    rejected (fail-closed) — there is no unauthenticated sealed path."""
    key = os.urandom(32)
    seed, _pub, iid = _make_sender()
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id=iid,
        payload_json="{}",
        space_content_key=key,
        signer_seed=seed,
    )
    d = env.to_dict()
    del d["outer_signature"]
    with pytest.raises(ValueError):
        SealedEnvelope.from_dict(d)


# ─── PQ-forward suite tag (#117 follow-up) ─────────────────────────────


def test_to_dict_includes_aead_suite():
    """Senders MUST ship the suite identifier so receivers can pick the
    primitive instead of guessing — Phase-2 forward-compat (see
    CLAUDE.md "Crypto wire shapes carry a `*_suite` tag")."""
    key = os.urandom(32)
    seed, _pub, _iid = _make_sender()
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id="x",
        payload_json="{}",
        space_content_key=key,
        signer_seed=seed,
    )
    d = env.to_dict()
    assert d["aead_suite"] == AEAD_SUITE_AESGCM_256


def test_from_dict_rejects_unknown_aead_suite():
    """Receivers MUST reject unknown suites — silent fallback would
    open a downgrade attack once a Phase-2 hybrid lands."""
    key = os.urandom(32)
    seed, _pub, _iid = _make_sender()
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id="x",
        payload_json="{}",
        space_content_key=key,
        signer_seed=seed,
    )
    d = env.to_dict()
    d["aead_suite"] = "future-pq-suite-not-yet-supported"
    with pytest.raises(UnsupportedAeadSuite):
        SealedEnvelope.from_dict(d)


def test_from_dict_accepts_missing_aead_suite_as_default():
    """First-revision senders that don't ship the field default to
    AES-256-GCM. Once every deployment ships it, the default-on-
    missing branch becomes the migration tripwire."""
    key = os.urandom(32)
    seed, _pub, _iid = _make_sender()
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id="x",
        payload_json="{}",
        space_content_key=key,
        signer_seed=seed,
    )
    d = env.to_dict()
    del d["aead_suite"]
    reconstructed = SealedEnvelope.from_dict(d)
    assert reconstructed.aead_suite == AEAD_SUITE_AESGCM_256
