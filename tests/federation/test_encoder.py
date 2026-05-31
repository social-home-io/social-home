"""Tests for :class:`FederationEncoder` raw-bytes AEAD (media payloads).

The string ``encrypt_payload`` / ``decrypt_payload`` round-trip is
covered elsewhere; this file pins the binary ``encrypt_bytes`` /
``decrypt_bytes`` used by the ``fed-media-v1`` channel.
"""

from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag

from socialhome.crypto import generate_identity_keypair
from socialhome.federation.encoder import FederationEncoder


def _encoder() -> FederationEncoder:
    return FederationEncoder(generate_identity_keypair().private_key)


def test_encrypt_bytes_round_trip():
    enc = _encoder()
    key = os.urandom(32)
    raw = b"\x00\x01\x02 the quick brown fox \xff\xfe" * 100
    blob = enc.encrypt_bytes(raw, key)
    # nonce(12) + ciphertext + 16-byte GCM tag — and NOT base64.
    assert len(blob) == 12 + len(raw) + 16
    assert enc.decrypt_bytes(blob, key) == raw


def test_encrypt_bytes_empty():
    enc = _encoder()
    key = os.urandom(32)
    blob = enc.encrypt_bytes(b"", key)
    assert enc.decrypt_bytes(blob, key) == b""


def test_encrypt_bytes_unique_nonce_per_call():
    enc = _encoder()
    key = os.urandom(32)
    raw = b"same plaintext"
    a = enc.encrypt_bytes(raw, key)
    b = enc.encrypt_bytes(raw, key)
    assert a[:12] != b[:12]  # fresh random nonce each call
    assert a != b


def test_decrypt_bytes_wrong_key_fails():
    enc = _encoder()
    blob = enc.encrypt_bytes(b"secret", os.urandom(32))
    with pytest.raises(InvalidTag):
        enc.decrypt_bytes(blob, os.urandom(32))


def test_decrypt_bytes_tampered_ciphertext_fails():
    enc = _encoder()
    key = os.urandom(32)
    blob = bytearray(enc.encrypt_bytes(b"secret payload", key))
    blob[20] ^= 0x01  # flip a ciphertext byte
    with pytest.raises(InvalidTag):
        enc.decrypt_bytes(bytes(blob), key)


def test_decrypt_bytes_too_short_for_nonce():
    enc = _encoder()
    with pytest.raises(ValueError):
        enc.decrypt_bytes(b"short", os.urandom(32))
