"""Sealed sender — encrypt the sender identity for GFS-relayed events (§4.3).

When a public-space event flows through the Global Federation Server
(GFS), the GFS needs to know:

* the **space_id** to route to subscribers,
* the **epoch** to pick the right key.

It does NOT need to know **which instance** sent it.  Sealed sender
hides ``from_instance`` from the GFS by encrypting it under the space's
content key (the same key used by :class:`SpaceContentEncryption`).

For a public/global space the content key is shared *widely* — every
subscriber holds it. Encryption alone therefore proves only that the
sealer had the key, not *who* sealed it: any key-holder could forge
content as any member, and a GFS could substitute one sealed blob for
another undetected. To close that, every envelope carries an
**outer_signature**: the sender Ed25519-signs a canonical,
domain-separated message binding the GFS-visible routing fields
(``space_id``, ``epoch``) AND both ciphertexts (``encrypted_sender``,
``encrypted_payload``) AND the ``aead_suite`` (see
:func:`_outer_signing_bytes`). The signature is produced with the
sender's long-term identity seed, never with the (shared) space key.

The recipient (in :func:`unseal_envelope`):

1. AEAD-decrypts ``encrypted_sender`` to learn the *claimed*
   ``sender_instance_id`` and ``encrypted_payload`` to get the content;
2. resolves that id to the sender's registered Ed25519 identity pubkey
   via ``sender_pk_lookup`` — an unknown sender is rejected
   (:class:`SealedSenderAuthError`), fail-closed;
3. binds the key to the claimed id —
   ``derive_instance_id(pk) == sender_instance_id`` — so a forger can't
   present some *other* valid key that hashes to the victim's id (160-bit
   collision resistance, mirrors the #596 mesh-route fix);
4. verifies ``outer_signature`` against that pubkey over the canonical
   message; a mismatch (forgery, tamper, or GFS substitution) raises.

Only after all four checks pass is the decrypted sender authenticated.
A GFS that drops/substitutes the signature, or a key-holder forging
content as another member, is therefore detected. A GFS still cannot
read the sender or payload (they remain AEAD-encrypted under the space
key it does not hold).

Wire format::

    {
      "sealed":            true,
      "space_id":          "sp-xyz",          # plaintext (routing)
      "epoch":             3,                 # plaintext (key selection)
      "encrypted_sender":  "<nonce>:<ct>",    # AES-256-GCM
      "encrypted_payload": "<nonce>:<ct>",    # space payload encryption
      "aead_suite":        "aesgcm-256",      # PQ-forward suite tag
      "outer_signature":   "<sig>"            # Ed25519 over the canonical
                                              # binding (required — an
                                              # envelope without it is
                                              # rejected by from_dict)
    }
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    sign_ed25519,
    verify_ed25519,
)


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


class SealedSenderAuthError(ValueError):
    """Raised when a sealed envelope's sender cannot be authenticated:
    an unknown sender, a pubkey that doesn't derive to the claimed
    ``sender_instance_id``, a malformed signature, or an
    ``outer_signature`` that fails verification. Always fail-closed —
    an unauthenticated sender is never returned to the caller."""


def _outer_signing_bytes(
    space_id: str,
    epoch: int,
    encrypted_sender: str,
    encrypted_payload: str,
    aead_suite: str,
) -> bytes:
    """Canonical, domain-separated message the outer signature covers.

    Binds every GFS-visible routing field AND both ciphertexts AND the
    AEAD suite tag, so a GFS that tampers with routing, substitutes a
    ciphertext, or downgrades the suite is detected at verify time.
    """
    return b"sealed-sender:v1:" + json.dumps(
        {
            "space_id": space_id,
            "epoch": epoch,
            "encrypted_sender": encrypted_sender,
            "encrypted_payload": encrypted_payload,
            "aead_suite": aead_suite,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@dataclass(slots=True, frozen=True)
class SealedEnvelope:
    """The structure produced by :func:`seal_envelope`."""

    space_id: str
    epoch: int
    encrypted_sender: str
    encrypted_payload: str
    #: Ed25519 signature (b64url) by the sender's identity key over
    #: :func:`_outer_signing_bytes`. Required — an envelope without one
    #: is rejected by :meth:`from_dict` (fail-closed; no unsigned path).
    outer_signature: str
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
            "outer_signature": self.outer_signature,
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
        # ``outer_signature`` is REQUIRED — an envelope without it is
        # unsigned, so we cannot authenticate the sender. Reject it here
        # (fail-closed) rather than down the line, so an unsigned blob
        # never reaches :func:`unseal_envelope`.
        sig = data.get("outer_signature")
        if not sig:
            raise ValueError("Malformed sealed envelope: missing outer_signature")
        try:
            return cls(
                space_id=str(data["space_id"]),
                epoch=int(data["epoch"]),
                encrypted_sender=str(data["encrypted_sender"]),
                encrypted_payload=str(data["encrypted_payload"]),
                outer_signature=str(sig),
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
    signer_seed: bytes,
) -> SealedEnvelope:
    """Encrypt sender + payload under the per-epoch space key and sign.

    ``space_content_key`` is the raw 32-byte AES key already unwrapped
    by :class:`SpaceContentEncryption`.  Callers should not pass the
    KEK-wrapped form — the wire-level KEK is for at-rest only.

    ``signer_seed`` is the sender's 32-byte Ed25519 identity seed; it
    produces the ``outer_signature`` that authenticates the sealed
    sender to the recipient. It MUST be the identity key whose pubkey
    derives to ``sender_instance_id`` — otherwise the recipient's
    derive-and-verify check rejects the envelope.
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
    encrypted_sender = _pack(sender_nonce, sender_ct)
    encrypted_payload = _pack(payload_nonce, payload_ct)
    outer_signature = b64url_encode(
        sign_ed25519(
            signer_seed,
            _outer_signing_bytes(
                space_id,
                epoch,
                encrypted_sender,
                encrypted_payload,
                AEAD_SUITE_AESGCM_256,
            ),
        )
    )
    return SealedEnvelope(
        space_id=space_id,
        epoch=epoch,
        encrypted_sender=encrypted_sender,
        encrypted_payload=encrypted_payload,
        outer_signature=outer_signature,
    )


@dataclass(slots=True, frozen=True)
class UnsealedContent:
    sender_instance_id: str
    payload: dict


def unseal_envelope(
    envelope: SealedEnvelope,
    *,
    space_content_key: bytes,
    sender_pk_lookup: Callable[[str], bytes | None],
) -> UnsealedContent:
    """Inverse of :func:`seal_envelope` — decrypt AND authenticate.

    ``sender_pk_lookup`` maps a decrypted ``sender_instance_id`` to that
    instance's registered Ed25519 identity public key bytes (or ``None``
    if the sender is unknown). The returned :class:`UnsealedContent` is
    only produced once the sender is authenticated:

    1. the claimed sender resolves to a pubkey (else
       :class:`SealedSenderAuthError`);
    2. that pubkey derives to the claimed id (else
       :class:`SealedSenderAuthError` — binds key↔id, mirrors #596);
    3. the ``outer_signature`` verifies against it (else
       :class:`SealedSenderAuthError`).

    AEAD failures (wrong key, tampered ciphertext, mutated routing
    AAD) raise the underlying ``cryptography`` exception, as before.
    """
    if len(space_content_key) != 32:
        raise ValueError("space_content_key must be 32 bytes")
    aead = AESGCM(space_content_key)
    aad = f"{envelope.space_id}:{envelope.epoch}".encode("utf-8")

    sender_nonce, sender_ct = _unpack(envelope.encrypted_sender)
    payload_nonce, payload_ct = _unpack(envelope.encrypted_payload)

    sender = aead.decrypt(sender_nonce, sender_ct, aad).decode("utf-8")
    payload = aead.decrypt(payload_nonce, payload_ct, aad).decode("utf-8")

    # ─── Authenticate the (now-decrypted) sender ────────────────────────
    pk = sender_pk_lookup(sender)
    if pk is None:
        raise SealedSenderAuthError(
            f"unknown sealed sender {sender!r}; cannot authenticate",
        )
    # Bind the key to the claimed id: a forger can't present some other
    # valid identity key that hashes to the victim's instance_id.
    try:
        derived = derive_instance_id(pk)
    except ValueError as exc:
        raise SealedSenderAuthError(
            f"sender pubkey for {sender!r} is malformed: {exc}",
        ) from exc
    if derived != sender:
        raise SealedSenderAuthError(
            f"sender pubkey derives to {derived!r}, not claimed {sender!r}",
        )
    try:
        sig = b64url_decode(envelope.outer_signature)
    except (ValueError, TypeError) as exc:
        raise SealedSenderAuthError(
            f"malformed outer_signature for {sender!r}: {exc}",
        ) from exc
    if not verify_ed25519(
        pk,
        _outer_signing_bytes(
            envelope.space_id,
            envelope.epoch,
            envelope.encrypted_sender,
            envelope.encrypted_payload,
            envelope.aead_suite,
        ),
        sig,
    ):
        raise SealedSenderAuthError(
            f"outer_signature failed verification for sender {sender!r}",
        )

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
