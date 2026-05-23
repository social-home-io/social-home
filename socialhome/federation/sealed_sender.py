"""Sealed sender — encrypt the sender identity for GFS-relayed events (§4.3).

When a public-space event flows through the Global Federation Server
(GFS), the GFS needs to know:

* the **space_id** to route to subscribers,
* the **epoch** to pick the right key.

It does NOT need to know **which instance** sent it.  Sealed sender
hides ``from_instance`` from the GFS by encrypting it under the space's
content key (the same key used by :class:`SpaceContentEncryption`).
The recipient decrypts the inner envelope, extracts the original
``from_instance`` and the signature, and verifies as usual.

Wire format::

    {
      "sealed":            true,
      "space_id":          "sp-xyz",          # plaintext (routing)
      "epoch":             3,                 # plaintext (key selection)
      "encrypted_sender":  "<nonce>:<ct>",    # AES-256-GCM
      "encrypted_payload": "<nonce>:<ct>",    # space payload encryption
      "outer_signature":   "<sig>"            # GFS-visible signature
                                              # (over space_id + epoch + ciphertexts)
    }

The recipient runs:

    sender = decrypt(encrypted_sender, space_key)
    payload = decrypt(encrypted_payload, space_key)
    verify(outer_signature, sender_pk_lookup(sender))

A GFS that drops or substitutes the outer_signature can be detected by
the recipient (signature mismatch). A GFS cannot read the sender field.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..crypto import b64url_decode, b64url_encode


_NONCE_BYTES = 12


#: Symmetric AEAD suite identifier shipped on the wire next to the
#: ciphertext. Mirrors the ``kem_suite`` / ``key_suite`` conventions
#: in ``routed_crypto.py`` / ``space_crypto_service.py`` — same
#: contract: receivers reject unknown values rather than fall back
#: to a default, so when a Phase-2 variant (ChaCha20-Poly1305 for
#: low-power receivers, or a PQ-protected wrapping) lands it's a
#: wire-additive change without breaking older receivers. See
#: CLAUDE.md "Crypto wire shapes carry a `*_suite` tag" for the
#: full rule.
AEAD_SUITE_AESGCM_256: str = "aesgcm-256"
SUPPORTED_AEAD_SUITES: frozenset[str] = frozenset({AEAD_SUITE_AESGCM_256})


class UnsupportedAeadSuite(ValueError):
    """Raised when a sealed envelope advertises an AEAD suite this
    build doesn't know. Receivers MUST reject rather than fall back
    to a default — otherwise a downgrade attack becomes possible
    once a Phase-2 hybrid scheme lands."""


@dataclass(slots=True, frozen=True)
class SealedEnvelope:
    """The structure produced by :func:`seal_envelope`."""

    space_id: str
    epoch: int
    encrypted_sender: str
    encrypted_payload: str
    #: AEAD primitive identifier — see :data:`AEAD_SUITE_AESGCM_256`.
    #: Defaults to today's only supported value so legacy in-memory
    #: instances (constructed without the field) stay valid.
    aead_suite: str = AEAD_SUITE_AESGCM_256

    def to_dict(self) -> dict:
        return {
            "sealed": True,
            "space_id": self.space_id,
            "epoch": self.epoch,
            "encrypted_sender": self.encrypted_sender,
            "encrypted_payload": self.encrypted_payload,
            "aead_suite": self.aead_suite,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SealedEnvelope":
        if not data.get("sealed"):
            raise ValueError("Envelope is not sealed")
        # First-revision senders that don't ship ``aead_suite`` default
        # to ``aesgcm-256`` — the only value this build knows. Once
        # every deployment ships the field, the default-on-missing
        # branch becomes the migration tripwire (older senders need
        # an upgrade).
        suite = str(data.get("aead_suite") or AEAD_SUITE_AESGCM_256)
        if suite not in SUPPORTED_AEAD_SUITES:
            raise UnsupportedAeadSuite(
                f"sealed envelope advertises unsupported aead_suite={suite!r}; "
                f"this build supports {sorted(SUPPORTED_AEAD_SUITES)!r}",
            )
        try:
            return cls(
                space_id=str(data["space_id"]),
                epoch=int(data["epoch"]),
                encrypted_sender=str(data["encrypted_sender"]),
                encrypted_payload=str(data["encrypted_payload"]),
                aead_suite=suite,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Malformed sealed envelope: {exc}") from exc


def seal_envelope(
    *,
    space_id: str,
    epoch: int,
    sender_instance_id: str,
    payload_json: str,
    space_content_key: bytes,
) -> SealedEnvelope:
    """Encrypt sender + payload under the per-epoch space key.

    ``space_content_key`` is the raw 32-byte AES key already unwrapped
    by :class:`SpaceContentEncryption`.  Callers should not pass the
    KEK-wrapped form — the wire-level KEK is for at-rest only.
    """
    if len(space_content_key) != 32:
        raise ValueError("space_content_key must be 32 bytes")
    aead = AESGCM(space_content_key)
    aad = f"{space_id}:{epoch}".encode("utf-8")

    sender_nonce = os.urandom(_NONCE_BYTES)
    payload_nonce = os.urandom(_NONCE_BYTES)

    sender_ct = aead.encrypt(
        sender_nonce,
        sender_instance_id.encode("utf-8"),
        aad,
    )
    payload_ct = aead.encrypt(
        payload_nonce,
        payload_json.encode("utf-8"),
        aad,
    )
    return SealedEnvelope(
        space_id=space_id,
        epoch=epoch,
        encrypted_sender=_pack(sender_nonce, sender_ct),
        encrypted_payload=_pack(payload_nonce, payload_ct),
    )


@dataclass(slots=True, frozen=True)
class UnsealedContent:
    sender_instance_id: str
    payload: dict


def unseal_envelope(
    envelope: SealedEnvelope,
    *,
    space_content_key: bytes,
) -> UnsealedContent:
    """Inverse of :func:`seal_envelope`."""
    if len(space_content_key) != 32:
        raise ValueError("space_content_key must be 32 bytes")
    aead = AESGCM(space_content_key)
    aad = f"{envelope.space_id}:{envelope.epoch}".encode("utf-8")

    sender_nonce, sender_ct = _unpack(envelope.encrypted_sender)
    payload_nonce, payload_ct = _unpack(envelope.encrypted_payload)

    sender = aead.decrypt(sender_nonce, sender_ct, aad).decode("utf-8")
    payload = aead.decrypt(payload_nonce, payload_ct, aad).decode("utf-8")
    return UnsealedContent(
        sender_instance_id=sender,
        payload=json.loads(payload),
    )


# ─── Internal pack / unpack ──────────────────────────────────────────────


def _pack(nonce: bytes, ct: bytes) -> str:
    return b64url_encode(nonce) + ":" + b64url_encode(ct)


def _unpack(wire: str) -> tuple[bytes, bytes]:
    try:
        nonce_b64, ct_b64 = wire.split(":", 1)
    except ValueError as exc:
        raise ValueError(f"Malformed sealed wire format: {wire!r}") from exc
    return b64url_decode(nonce_b64), b64url_decode(ct_b64)
