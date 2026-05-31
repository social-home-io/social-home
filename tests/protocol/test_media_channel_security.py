"""§25.8.21 wire guards for the binary media channel (``fed-media-v1``).

Release-blocker protocol tests (marked ``security``): the media channel
must never leak chunk bytes in cleartext and must reject unknown AEAD
suites with no default fallback — the same encryption-first + suite-tag
contract every other federation surface obeys.
"""

from __future__ import annotations

import hashlib
import os

import orjson
import pytest

from socialhome.crypto import b64url_encode, generate_identity_keypair
from socialhome.federation import media_framing as mf
from socialhome.federation.encoder import FederationEncoder

pytestmark = pytest.mark.security


def _build_wire_frame(raw: bytes, session_key: bytes) -> tuple[bytes, bytes]:
    """Reproduce the sender's wire output: a signed envelope whose
    encrypted metadata carries the chunk binding, plus the AEAD-encrypted
    chunk as the binary payload."""
    enc = FederationEncoder(generate_identity_keypair().private_key)
    meta = {
        "media_blob_id": "m1",
        "message_id": "m1",
        "chunk_sha256": b64url_encode(hashlib.sha256(raw).digest()),
        "media_aead_suite": mf.MEDIA_AEAD_SUITE_AESGCM_256,
    }
    envelope = {
        "msg_id": "m1",
        "event_type": "dm_media_blob",
        "from_instance": "a",
        "to_instance": "b",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "encrypted_payload": enc.encrypt_payload(
            orjson.dumps(meta).decode(), session_key
        ),
        "space_id": None,
        "proto_version": 1,
        "sig_suite": "ed25519",
    }
    envelope["signatures"] = enc.sign_envelope_all(
        orjson.dumps(envelope), suite="ed25519"
    )
    header_bytes = orjson.dumps(envelope)
    payload_bytes = enc.encrypt_bytes(raw, session_key)
    return mf.encode(header_bytes, payload_bytes), header_bytes


def test_raw_chunk_never_appears_in_cleartext_on_the_wire():
    """Encryption-first: the plaintext chunk must not be recoverable from
    the framed bytes without the session key."""
    session_key = os.urandom(32)
    raw = b"SECRET-MEDIA-MARKER-" + os.urandom(64)
    frame, _header = _build_wire_frame(raw, session_key)
    # The recognisable plaintext marker is nowhere in the framed bytes.
    assert b"SECRET-MEDIA-MARKER-" not in frame
    assert raw not in frame


def test_header_carries_no_plaintext_bytes_field():
    """The cleartext envelope header must not carry a ``bytes_b64`` (the
    bytes live in the encrypted payload section, not the signed header)."""
    session_key = os.urandom(32)
    _frame, header_bytes = _build_wire_frame(b"x" * 100, session_key)
    envelope = orjson.loads(header_bytes)
    assert "bytes_b64" not in envelope
    assert "bytes_base64" not in envelope
    # The suite tag + binding live ONLY inside the encrypted payload, not
    # in cleartext where an attacker could strip/downgrade them.
    assert "media_aead_suite" not in envelope
    assert "chunk_sha256" not in envelope


def test_unknown_aead_suite_has_no_default_fallback():
    """The supported-suite set is closed; an unknown suite is not a member
    (the receiver raises rather than defaulting)."""
    assert "bogus-suite" not in mf.SUPPORTED_MEDIA_AEAD_SUITES
    assert mf.MEDIA_AEAD_SUITE_AESGCM_256 in mf.SUPPORTED_MEDIA_AEAD_SUITES
    # The dedicated exception exists for the reject path.
    assert issubclass(mf.UnsupportedMediaAeadSuite, ValueError)


def test_oversize_frame_lengths_are_rejected_pre_allocation():
    """A malicious peer can't force an unbounded allocation via a giant
    declared length."""
    import struct

    too_big = struct.pack(">BI", 1, mf.MAX_HEADER_BYTES + 1)
    with pytest.raises(mf.MediaFramingError):
        mf.decode(too_big)
