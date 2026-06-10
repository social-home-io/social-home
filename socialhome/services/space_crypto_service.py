"""SpaceContentEncryption — per-space epoch-keyed AES-256-GCM (§4.3, §25.8.20–21).

Outbound space content is encrypted with the *current* epoch key; the
epoch number travels in the federation envelope (plaintext) so the
receiver can pick the right decryption key.

Key rotation:

* :meth:`SpaceContentEncryption.rotate_epoch` mints a new 32-byte AES
  key, KEK-encrypts it, persists it, and returns the new epoch
  number.  Triggers: member ban, admin departure, admin promotion,
  scheduled rotation.
* Old epoch keys are kept indefinitely so historical content remains
  decryptable for legitimate readers.

Encryption-first rule (§25.8.21): every space-scoped event MUST be
encrypted unless the federation service needs the field in plaintext
for routing. Routing fields (event_type, from/to, space_id, epoch)
travel in the clear; everything else lands in
``encrypted_payload``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..federation.sealed_sender import (
    SealedEnvelope,
    UnsealedContent,
    seal_envelope,
    unseal_envelope,
)

from ..crypto import (
    b64url_decode,
    b64url_encode,
    derive_space_id,
    generate_space_keypair,
    sign_ed25519,
    verify_ed25519,
)
from ..domain.events import SpaceContentKeyImported
from ..infrastructure.event_bus import EventBus
from ..infrastructure.key_manager import KeyManager
from ..repositories.federation_repo import AbstractFederationRepo
from ..repositories.space_key_repo import (
    AbstractSpaceKeyRepo,
    SpaceKey,
)
from .bus_publisher import BusPublisherMixin

log = logging.getLogger(__name__)


_AES_KEY_BYTES = 32
_GCM_NONCE_BYTES = 12


#: Symmetric content-key suite identifier shipped alongside the key
#: bytes on the §D1b handoff. Mirrors the ``kem_suite`` convention in
#: :mod:`socialhome.federation.routed_crypto` so the wire shape grows
#: forward-compatibly. When we ever introduce a parallel scheme
#: (e.g. ChaCha20-Poly1305 for low-power receivers, or a PQ-protected
#: variant once Phase-2 of ``docs/crypto.md`` lands), this constant
#: gains a sibling and receivers reject anything they don't know.
#:
#: The AES-256 key itself is already PQ-symmetric-resilient — Grover's
#: algorithm halves effective security to 128 bits, which is still
#: above the 112-bit floor. PQ migration in this codebase is therefore
#: about the *delivery channel* (the §D1b envelope around this
#: payload, see ``docs/crypto.md`` Phase-2), not the AEAD primitive.
KEY_SUITE_AESGCM_256: str = "aesgcm-256"
SUPPORTED_KEY_SUITES: frozenset[str] = frozenset({KEY_SUITE_AESGCM_256})


class UnsupportedKeySuite(ValueError):
    """Raised when an inbound space-content-key payload advertises a
    suite this build doesn't know. Receivers MUST reject rather than
    fall back to a default — otherwise a downgrade attack becomes
    possible once a Phase-2 hybrid scheme lands."""


class SpaceContentEncryption(BusPublisherMixin):
    """Encrypt/decrypt space content under per-epoch AES-256-GCM keys.

    Parameters
    ----------
    space_key_repo:
        Persistence for epoch keys (KEK-encrypted ciphertext at rest).
    key_manager:
        KEK used to wrap epoch keys before persistence.
    identity_seed:
        This instance's 32-byte Ed25519 identity seed. Used to sign the
        ``outer_signature`` on sealed-sender envelopes (:meth:`seal_for_gfs`)
        so a GFS-relayed event is authenticated to the recipient. Optional
        only so legacy/test constructions that never seal stay valid;
        :meth:`seal_for_gfs` raises if it wasn't supplied.
    federation_repo:
        Resolves a remote ``instance_id`` to its registered Ed25519
        identity pubkey, so :meth:`unseal_from_gfs` can authenticate a
        sealed sender out of the box. Optional only so legacy/test
        constructions that never unseal stay valid; :meth:`unseal_from_gfs`
        requires either this repo OR an explicit ``sender_pk_lookup``.
    """

    __slots__ = ("_repo", "_kek", "_bus", "_identity_seed", "_fed_repo")

    def __init__(
        self,
        space_key_repo: AbstractSpaceKeyRepo,
        key_manager: KeyManager,
        *,
        bus: EventBus | None = None,
        identity_seed: bytes | None = None,
        federation_repo: AbstractFederationRepo | None = None,
    ) -> None:
        self._repo = space_key_repo
        self._kek = key_manager
        #: Optional — when wired, ``import_key`` publishes
        #: :class:`SpaceContentKeyImported` so :class:`PendingDecryptsCache`
        #: can drain any sync chunks that arrived before this epoch's
        #: key was available (#122, out-of-order key arrival).
        self._bus = bus
        self._identity_seed = identity_seed
        self._fed_repo = federation_repo

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def initialise_for_space(self, space_id: str) -> int:
        """Mint epoch 0 for a brand-new space. Returns the epoch number.

        No-op if the space already has at least one epoch key.
        """
        existing = await self._repo.get_latest(space_id)
        if existing is not None:
            return existing.epoch
        return await self.rotate_epoch(space_id)

    async def rotate_epoch(self, space_id: str) -> int:
        """Generate a fresh AES key, persist it, return the new epoch."""
        epoch = await self._repo.next_epoch(space_id)
        raw = AESGCM.generate_key(bit_length=256)
        wrapped = self._kek.encrypt(raw, associated_data=space_id.encode("utf-8"))
        await self._repo.save(
            SpaceKey(
                space_id=space_id,
                epoch=epoch,
                content_key_hex=wrapped,
            )
        )
        log.info("space_crypto: rotated %s to epoch %d", space_id, epoch)
        return epoch

    async def get_current_epoch(self, space_id: str) -> int | None:
        latest = await self._repo.get_latest(space_id)
        return latest.epoch if latest is not None else None

    # ─── Cross-instance handoff (#117) ────────────────────────────────────

    async def export_current_key(self, space_id: str) -> tuple[int, bytes] | None:
        """Return ``(epoch, raw_aes_key)`` for the §D1b invite envelope.

        Unwraps from KEK so the caller can hand the bytes to the
        federation layer. The envelope is itself encrypted to the
        invitee instance (§D1b zero-leak), so the key never leaves
        this process in plaintext anywhere — but it IS the canonical
        secret that decrypts every event in the space, so callers
        MUST ONLY use this return value inline within an encrypted
        envelope payload. Never persist, never log.

        Returns ``None`` when the space has no keys yet — the
        receiver then falls back to its own ``initialise_for_space``
        on first decrypt attempt (which will fail until the key
        actually federates later, but that's an explicit failure
        rather than a silent decrypt-with-zeros).
        """
        latest = await self._repo.get_latest(space_id)
        if latest is None:
            return None
        raw = self._kek.decrypt(
            latest.content_key_hex,
            associated_data=space_id.encode("utf-8"),
        )
        return latest.epoch, raw

    async def import_key(
        self,
        space_id: str,
        epoch: int,
        raw_key: bytes,
    ) -> None:
        """Persist a key received from a federated peer.

        KEK-wraps with the *receiver's* key manager so the local
        ``space_keys`` row matches the at-rest invariant. Idempotent —
        a repeated import for the same ``(space_id, epoch)`` upserts.
        """
        if len(raw_key) != 32:
            raise ValueError("space content key must be 32 bytes")
        wrapped = self._kek.encrypt(
            raw_key,
            associated_data=space_id.encode("utf-8"),
        )
        await self._repo.save(
            SpaceKey(
                space_id=space_id,
                epoch=epoch,
                content_key_hex=wrapped,
            )
        )
        log.info(
            "space_crypto: imported epoch %d for %s from peer",
            epoch,
            space_id,
        )
        await self._emit(
            SpaceContentKeyImported(space_id=space_id, epoch=epoch),
        )

    # ─── Encrypt / decrypt ────────────────────────────────────────────────

    async def encrypt(self, space_id: str, plaintext: bytes) -> tuple[int, str]:
        """Encrypt under the current epoch. Returns ``(epoch, ciphertext)``.

        Raises :class:`RuntimeError` if no epoch key exists yet — per the
        encryption-first rule in CLAUDE.md, callers should never silently
        fall back to plaintext.
        """
        latest = await self._repo.get_latest(space_id)
        if latest is None:
            raise RuntimeError(
                f"SpaceContentEncryption: no key for space {space_id!r}; "
                "call initialise_for_space() first."
            )
        raw = self._kek.decrypt(
            latest.content_key_hex,
            associated_data=space_id.encode("utf-8"),
        )
        nonce = os.urandom(_GCM_NONCE_BYTES)
        aead = AESGCM(raw)
        ct = aead.encrypt(nonce, plaintext, space_id.encode("utf-8"))
        wire = b64url_encode(nonce) + ":" + b64url_encode(ct)
        return latest.epoch, wire

    async def decrypt(
        self,
        space_id: str,
        epoch: int,
        ciphertext: str,
    ) -> bytes:
        """Decrypt under the specified epoch's key."""
        key = await self._repo.get(space_id, epoch)
        if key is None:
            raise RuntimeError(
                f"SpaceContentEncryption: missing epoch {epoch} for space {space_id!r}"
            )
        raw = self._kek.decrypt(
            key.content_key_hex,
            associated_data=space_id.encode("utf-8"),
        )
        try:
            nonce_b64, ct_b64 = ciphertext.split(":", 1)
        except ValueError as exc:
            raise ValueError("Malformed space ciphertext") from exc
        nonce = b64url_decode(nonce_b64)
        ct = b64url_decode(ct_b64)
        aead = AESGCM(raw)
        return aead.decrypt(nonce, ct, space_id.encode("utf-8"))

    # ─── Sync chunks (§25.6 direct space sync) ──────────────────────────

    async def encrypt_chunk(
        self,
        *,
        space_id: str,
        sync_id: str,
        plaintext: bytes,
    ) -> tuple[int, str]:
        """AES-256-GCM-encrypt a sync chunk under the current epoch key.

        AAD binds to ``space_id:epoch:sync_id`` so a chunk lifted from
        one session can't be replayed into another (§25.8.18). Returns
        ``(epoch, ciphertext)``.
        """
        latest = await self._repo.get_latest(space_id)
        if latest is None:
            raise RuntimeError(
                f"SpaceContentEncryption: no key for space {space_id!r}; "
                "call initialise_for_space() first."
            )
        raw = self._kek.decrypt(
            latest.content_key_hex,
            associated_data=space_id.encode("utf-8"),
        )
        nonce = os.urandom(_GCM_NONCE_BYTES)
        aad = f"{space_id}:{latest.epoch}:{sync_id}".encode("utf-8")
        aead = AESGCM(raw)
        ct = aead.encrypt(nonce, plaintext, aad)
        wire = b64url_encode(nonce) + ":" + b64url_encode(ct)
        return latest.epoch, wire

    async def decrypt_chunk(
        self,
        *,
        space_id: str,
        epoch: int,
        sync_id: str,
        ciphertext: str,
    ) -> bytes:
        """Inverse of :meth:`encrypt_chunk`. Raises :class:`RuntimeError`
        on missing epoch or :class:`cryptography.exceptions.InvalidTag`
        when the AAD doesn't match."""
        key = await self._repo.get(space_id, epoch)
        if key is None:
            raise RuntimeError(
                f"SpaceContentEncryption: missing epoch {epoch} for space {space_id!r}"
            )
        raw = self._kek.decrypt(
            key.content_key_hex,
            associated_data=space_id.encode("utf-8"),
        )
        try:
            nonce_b64, ct_b64 = ciphertext.split(":", 1)
        except ValueError as exc:
            raise ValueError("Malformed space ciphertext") from exc
        nonce = b64url_decode(nonce_b64)
        ct = b64url_decode(ct_b64)
        aad = f"{space_id}:{epoch}:{sync_id}".encode("utf-8")
        aead = AESGCM(raw)
        return aead.decrypt(nonce, ct, aad)

    # ─── Sealed sender (GFS-relayed events §24.10) ──────────────────────

    async def seal_for_gfs(
        self,
        *,
        space_id: str,
        sender_instance_id: str,
        payload_json: str,
    ) -> SealedEnvelope:
        """Wrap an outbound public/global-space event so the GFS relay
        can route it without learning ``from_instance``.

        Returns a :class:`~socialhome.federation.sealed_sender.SealedEnvelope`
        that callers serialise into the ``GFS_POST_RELAY`` payload. The
        space's per-epoch key is unwrapped from the KEK before each call —
        callers never see the raw key material.
        """
        if self._identity_seed is None:
            raise RuntimeError(
                "seal_for_gfs: no identity_seed wired; cannot sign the "
                "outer_signature that authenticates a sealed sender.",
            )
        latest = await self._repo.get_latest(space_id)
        if latest is None:
            raise RuntimeError(
                f"seal_for_gfs: no epoch key for space {space_id!r}",
            )
        raw_key = self._kek.decrypt(
            latest.content_key_hex,
            associated_data=space_id.encode("utf-8"),
        )
        sealed = seal_envelope(
            space_id=space_id,
            epoch=latest.epoch,
            sender_instance_id=sender_instance_id,
            payload_json=payload_json,
            space_content_key=raw_key,
            signer_seed=self._identity_seed,
        )
        return sealed

    async def unseal_from_gfs(
        self,
        envelope: SealedEnvelope,
        *,
        sender_pk_lookup: Callable[[str], bytes | None] | None = None,
    ) -> UnsealedContent:
        """Inverse of :meth:`seal_for_gfs` — fetch the matching epoch
        key, decrypt, AND authenticate the sealed sender.

        The sealed sender is verified against its registered Ed25519
        identity pubkey (see :func:`unseal_envelope`). The pubkey
        resolver defaults to a lookup backed by ``federation_repo``
        (so the service is usable out of the box); a caller may pass an
        explicit ``sender_pk_lookup`` to override — useful when the
        sender isn't yet in ``remote_instances`` (e.g. a not-yet-paired
        public-space peer the caller resolves another way). One of the
        two MUST be available, else we cannot authenticate and raise.
        """
        key = await self._repo.get(envelope.space_id, envelope.epoch)
        if key is None:
            raise RuntimeError(
                f"unseal_from_gfs: missing epoch {envelope.epoch} for space "
                f"{envelope.space_id!r}",
            )
        raw_key = self._kek.decrypt(
            key.content_key_hex,
            associated_data=envelope.space_id.encode("utf-8"),
        )

        lookup = sender_pk_lookup
        if lookup is None:
            if self._fed_repo is None:
                raise RuntimeError(
                    "unseal_from_gfs: no federation_repo wired and no "
                    "sender_pk_lookup supplied; cannot authenticate the "
                    "sealed sender.",
                )
            # Resolve the claimed sender to a pubkey via remote_instances.
            # ``unseal_envelope`` only asks for ONE id (the decrypted
            # sender), so pre-load the members of this space into a
            # sync map the sync callable can read. A sender not in the
            # map resolves to ``None`` → SealedSenderAuthError, fail-closed.
            members = await self._fed_repo.list_instances_in_space(
                envelope.space_id,
            )
            pk_by_id = {
                inst.id: bytes.fromhex(inst.remote_identity_pk)
                for inst in members
                if inst.remote_identity_pk
            }
            lookup = pk_by_id.get

        unsealed = unseal_envelope(
            envelope,
            space_content_key=raw_key,
            sender_pk_lookup=lookup,
        )
        return unsealed


# ─── Space identity helpers ──────────────────────────────────────────────


def create_space_identity() -> tuple[bytes, bytes, str]:
    """Mint a fresh space identity. Returns ``(seed, public_key, space_id)``.

    Convenience wrapper around :func:`generate_space_keypair` and
    :func:`derive_space_id`.
    """
    kp = generate_space_keypair()
    return kp.private_key, kp.public_key, derive_space_id(kp.public_key)


def sign_space_config(payload: bytes, *, space_seed: bytes) -> str:
    """Ed25519-sign a serialised SpaceConfigEvent (§4.3.4)."""
    sig = sign_ed25519(space_seed, payload)
    return b64url_encode(sig)


def verify_space_config(
    payload: bytes,
    signature_b64: str,
    *,
    space_public_key: bytes,
) -> bool:
    """Verify a config event's Ed25519 signature."""
    try:
        sig = b64url_decode(signature_b64)
    except Exception:
        return False
    return verify_ed25519(space_public_key, payload, sig)


# ─── Space-authority signature primitives ─────────────────────────────────
#
# A *space-authority* event is signed with the space's Ed25519 private seed
# (Phase 0, ``space_repo.get_space_seed`` / ``ensure_space_seed``) rather than
# any single household's key. Any household that legitimately holds the seed
# (the owner, or a delegated admin per Phase 1) can emit one, and EVERY
# receiver trusts it by verifying against ``spaces.identity_public_key`` —
# independent of which household relayed it. That decoupling is what lets a
# space keep working while the owner is offline (the "owner-offline spaces"
# epic).
#
# The verifier is a pure function over the space's PUBLIC key, so the §24.11
# inbound handler can authenticate the event without ever holding the seed.

#: Suite identifier for the space-authority signature, shipped on the wire
#: alongside the signature so the algorithm can swap without breaking older
#: receivers (crypto-suite rule). Today Ed25519; Phase-2 of ``docs/crypto.md``
#: introduces a sibling ``"ed25519+mldsa65"`` (parallel PQ signature). Receivers
#: MUST reject any suite not in ``SUPPORTED_AUTHORITY_SIG_SUITES`` — never fall
#: back to a default, or a downgrade attack becomes possible.
AUTHORITY_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_AUTHORITY_SIG_SUITES: frozenset[str] = frozenset(
    {AUTHORITY_SIG_SUITE_ED25519}
)


class UnsupportedAuthoritySuite(ValueError):
    """Raised when a space-authority signature advertises a suite this build
    doesn't know. Receivers MUST reject rather than fall back to a default."""


#: The two wire fields :func:`sign_authority_event` produces and the sender
#: merges into the event payload. The signature is computed over the payload
#: with these removed, so the verifier MUST strip the SAME two keys before
#: calling :func:`verify_authority_event` — otherwise the canonical bytes
#: differ and a legitimate signature fails. Naming them once here keeps both
#: sides in lock-step.
_AUTHORITY_SIG_FIELDS: frozenset[str] = frozenset(
    {"authority_sig", "authority_sig_suite"}
)


def strip_authority_sig_fields(payload: dict) -> dict:
    """Return a copy of ``payload`` without the two authority-signature fields.

    The signer signs over the *bare* payload (no signature fields) and merges
    ``authority_sig`` / ``authority_sig_suite`` in afterwards; the verifier
    must reconstruct those exact bare bytes by stripping them back out. Using
    this single helper on BOTH sides guarantees the canonical signing bytes
    match. Returns a new dict — the input is never mutated.
    """
    return {k: v for k, v in payload.items() if k not in _AUTHORITY_SIG_FIELDS}


def authority_signing_bytes(
    *,
    event_type: str,
    space_id: str,
    payload: dict,
) -> bytes:
    """Canonical, domain-separated message bytes for a space-authority event.

    Binds the event type + space id + the FULL payload under a versioned
    domain-separation prefix so a signature can't be lifted onto a different
    event type, a different space, or a mutated payload. ``sort_keys`` +
    compact separators make the encoding canonical — equivalent payloads
    (same keys/values, any insertion order) produce identical bytes, so
    signer and verifier always agree.
    """
    body = json.dumps(
        {"event_type": event_type, "space_id": space_id, "payload": payload},
        separators=(",", ":"),
        sort_keys=True,
    )
    return b"space-authority:v1:" + body.encode()


def sign_authority_event(
    *,
    event_type: str,
    space_id: str,
    payload: dict,
    space_seed: bytes,
) -> dict:
    """Sign a space-authority event with the space's Ed25519 seed.

    Returns the wire fields to merge into the outgoing event:
    ``authority_sig`` (b64url) + ``authority_sig_suite``.
    """
    sig = sign_ed25519(
        space_seed,
        authority_signing_bytes(
            event_type=event_type, space_id=space_id, payload=payload
        ),
    )
    return {
        "authority_sig": b64url_encode(sig),
        "authority_sig_suite": AUTHORITY_SIG_SUITE_ED25519,
    }


def verify_authority_event(
    *,
    event_type: str,
    space_id: str,
    payload: dict,
    authority_sig: str,
    authority_sig_suite: str,
    space_public_key: bytes,
) -> bool:
    """Verify a space-authority signature against the space's public key.

    Raises :class:`UnsupportedAuthoritySuite` for an unknown suite (no default
    fallback). Returns ``False`` for a malformed signature or a verification
    failure. Pure function — needs only the space PUBLIC key, never the seed.
    """
    if authority_sig_suite not in SUPPORTED_AUTHORITY_SIG_SUITES:
        raise UnsupportedAuthoritySuite(authority_sig_suite)
    try:
        sig = b64url_decode(authority_sig)
    except Exception:
        return False
    return verify_ed25519(
        space_public_key,
        authority_signing_bytes(
            event_type=event_type, space_id=space_id, payload=payload
        ),
        sig,
    )
