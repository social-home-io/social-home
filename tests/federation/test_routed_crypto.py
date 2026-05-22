"""Tests for the SPACE_ROUTED inner-payload sealing primitives.

Mirrors the property tests we'd write for the pairing-coordinator
key derivation: round-trip, directional-key mismatch detection,
AAD-binding (a ciphertext for one route_id can't decrypt as
another), and forward-vs-reply separation.
"""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from socialhome.federation import routed_crypto as rc


# ── Round-trip ─────────────────────────────────────────────────────────


def _ephemeral_pair() -> tuple[str, str, str, str]:
    """Convenience — returns ``(origin_priv, origin_pub, target_priv,
    target_pub)`` for use in tests."""
    o_priv, o_pub = rc.generate_ephemeral_keypair()
    t_priv, t_pub = rc.generate_ephemeral_keypair()
    return o_priv, o_pub, t_priv, t_pub


def test_seal_inner_round_trip():
    """Forward direction: origin seals → target unseals → plaintext."""
    o_priv, o_pub, t_priv, t_pub = _ephemeral_pair()
    sealed = rc.seal_inner_payload(
        inner_payload_json='{"token":"abc","user":"alice"}',
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=t_pub,
        route_id="r-1",
        inner_event_type="space_invite_token_redeem",
    )
    decoded = rc.unseal_inner_payload(
        sealed=sealed,
        target_eph_priv_b64=t_priv,
        route_id="r-1",
        inner_event_type="space_invite_token_redeem",
    )
    assert decoded == '{"token":"abc","user":"alice"}'


def test_seal_reply_round_trip():
    """Reply direction: target seals → origin unseals → plaintext."""
    o_priv, o_pub, t_priv, t_pub = _ephemeral_pair()
    sealed = rc.seal_reply_payload(
        inner_payload_json='{"space_id":"s-1","role":"member"}',
        target_eph_priv_b64=t_priv,
        target_eph_pub_b64=t_pub,
        origin_eph_pub_b64=o_pub,
        route_id="r-1",
        inner_event_type="space_invite_token_redeem",
    )
    decoded = rc.unseal_reply_payload(
        sealed=sealed,
        origin_eph_priv_b64=o_priv,
        route_id="r-1",
        inner_event_type="space_invite_token_redeem",
    )
    assert decoded == '{"space_id":"s-1","role":"member"}'


# ── Negative paths ─────────────────────────────────────────────────────


def test_relay_cannot_decrypt():
    """A relay holds neither ephemeral private half → can't decrypt
    even with full knowledge of both ephemeral pubs."""
    o_priv, o_pub, _t_priv, t_pub = _ephemeral_pair()
    sealed = rc.seal_inner_payload(
        inner_payload_json='{"secret":"don\'t look"}',
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=t_pub,
        route_id="r-1",
        inner_event_type="space_invite_token_redeem",
    )
    # Relay generates its own keypair, both pubs are public, but it
    # has neither private half — so any DH it computes yields a
    # different shared secret than origin+target derived.
    r_priv, _r_pub = rc.generate_ephemeral_keypair()
    with pytest.raises(InvalidTag):
        rc.unseal_inner_payload(
            sealed=sealed,
            target_eph_priv_b64=r_priv,  # wrong priv
            route_id="r-1",
            inner_event_type="space_invite_token_redeem",
        )


def test_aad_route_id_binding():
    """A ciphertext sealed for one route_id can't be replayed under a
    different route_id — AAD mismatch fails the AEAD tag."""
    o_priv, o_pub, t_priv, t_pub = _ephemeral_pair()
    sealed = rc.seal_inner_payload(
        inner_payload_json='{"token":"t-1"}',
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=t_pub,
        route_id="r-1",
        inner_event_type="space_invite_token_redeem",
    )
    with pytest.raises(InvalidTag):
        rc.unseal_inner_payload(
            sealed=sealed,
            target_eph_priv_b64=t_priv,
            route_id="r-2",  # different route_id
            inner_event_type="space_invite_token_redeem",
        )


def test_aad_event_type_binding():
    """A ciphertext sealed for one inner_event_type can't be replayed
    under a different inner_event_type — AAD mismatch fails the AEAD
    tag."""
    o_priv, o_pub, t_priv, t_pub = _ephemeral_pair()
    sealed = rc.seal_inner_payload(
        inner_payload_json='{"token":"t-1"}',
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=t_pub,
        route_id="r-1",
        inner_event_type="space_invite_token_redeem",
    )
    with pytest.raises(InvalidTag):
        rc.unseal_inner_payload(
            sealed=sealed,
            target_eph_priv_b64=t_priv,
            route_id="r-1",
            inner_event_type="space_post_created",  # different event
        )


def test_forward_ciphertext_cannot_be_replayed_as_ack():
    """A forward-direction ciphertext can't be passed off as a reply —
    forward uses origin→target key, reply uses target→origin key, and
    the AAD has an ``|ack`` suffix on the reply side."""
    o_priv, o_pub, t_priv, t_pub = _ephemeral_pair()
    sealed = rc.seal_inner_payload(
        inner_payload_json='{"token":"t-1"}',
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=t_pub,
        route_id="r-1",
        inner_event_type="space_invite_token_redeem",
    )
    # Try to decrypt the FORWARD ciphertext as if it were a REPLY —
    # the origin would use ``unseal_reply_payload`` with its own
    # private half. That key is target→origin, not origin→target, so
    # the AEAD tag fails.
    with pytest.raises(InvalidTag):
        rc.unseal_reply_payload(
            sealed=sealed,
            origin_eph_priv_b64=o_priv,
            route_id="r-1",
            inner_event_type="space_invite_token_redeem",
        )


def test_directional_keys_swap_on_role():
    """Origin's send_key equals target's recv_key, and vice versa —
    the invariant the directional-key derivation gates on."""
    o_priv, o_pub, t_priv, t_pub = _ephemeral_pair()
    o_send, o_recv = rc.derive_directional_keys(
        my_priv_b64=o_priv,
        peer_pub_b64=t_pub,
        is_origin=True,
    )
    t_send, t_recv = rc.derive_directional_keys(
        my_priv_b64=t_priv,
        peer_pub_b64=o_pub,
        is_origin=False,
    )
    assert o_send == t_recv
    assert o_recv == t_send


def test_missing_sealed_fields_raise_valueerror():
    """Malformed wire payload (missing fields) raises a clean
    ValueError rather than KeyError."""
    o_priv, _o_pub, _t_priv, _t_pub = _ephemeral_pair()
    with pytest.raises(ValueError):
        rc.unseal_inner_payload(
            sealed={"origin_eph_pk": "x"},  # missing nonce + ciphertext
            target_eph_priv_b64=o_priv,
            route_id="r-1",
            inner_event_type="space_invite_token_redeem",
        )


def test_expired_helper():
    """``expired`` returns True past the TTL window."""
    import time

    now = time.monotonic()
    assert not rc.expired(now, 60.0)
    assert rc.expired(now - 120.0, 60.0)


def test_nonce_is_fresh_per_seal():
    """Two seals of the same plaintext under the same key produce
    different ciphertexts — AES-GCM requires unique nonces and this
    is the regression-pin that we draw a fresh one each call."""
    o_priv, o_pub, _t_priv, t_pub = _ephemeral_pair()
    a = rc.seal_inner_payload(
        inner_payload_json='{"x":1}',
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=t_pub,
        route_id="r-1",
        inner_event_type="t",
    )
    b = rc.seal_inner_payload(
        inner_payload_json='{"x":1}',
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=t_pub,
        route_id="r-1",
        inner_event_type="t",
    )
    assert a["nonce"] != b["nonce"]
    assert a["ciphertext"] != b["ciphertext"]


def test_kem_suite_in_wire_shape():
    """Both seal paths set ``kem_suite`` on the wire — receivers
    check the field on unseal so PQ migration (Phase 2) becomes a
    suite-bump, not a wire-shape break."""
    o_priv, o_pub, t_priv, t_pub = _ephemeral_pair()
    fwd = rc.seal_inner_payload(
        inner_payload_json="{}",
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=t_pub,
        route_id="r-1",
        inner_event_type="t",
    )
    assert fwd["kem_suite"] == rc.KEM_SUITE_X25519
    reply = rc.seal_reply_payload(
        inner_payload_json="{}",
        target_eph_priv_b64=t_priv,
        target_eph_pub_b64=t_pub,
        origin_eph_pub_b64=o_pub,
        route_id="r-1",
        inner_event_type="t",
    )
    assert reply["kem_suite"] == rc.KEM_SUITE_X25519


def test_unknown_kem_suite_rejected():
    """A future ``kem_suite=mlkem768`` envelope arriving at a build
    that only knows ``x25519`` must be rejected, not silently
    downgraded — otherwise a hostile peer could force every receiver
    onto the weakest known suite."""
    o_priv, o_pub, t_priv, t_pub = _ephemeral_pair()
    sealed = rc.seal_inner_payload(
        inner_payload_json="{}",
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=t_pub,
        route_id="r-1",
        inner_event_type="t",
    )
    sealed["kem_suite"] = "mlkem768-future"
    with pytest.raises(rc.UnsupportedKemSuite):
        rc.unseal_inner_payload(
            sealed=sealed,
            target_eph_priv_b64=t_priv,
            route_id="r-1",
            inner_event_type="t",
        )
    with pytest.raises(rc.UnsupportedKemSuite):
        rc.unseal_reply_payload(
            sealed=sealed,
            origin_eph_priv_b64=o_priv,
            route_id="r-1",
            inner_event_type="t",
        )
