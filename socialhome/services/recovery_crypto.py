"""Recovery Kit codec — passphrase-sealed off-box backup of the trust layer.

A "Recovery Kit" (``.shrk`` file) lets a household protect its identity /
trust-layer material behind a user-chosen passphrase and store it off-box
(a USB stick, a password manager, a print-out QR). This module is the PURE
crypto codec: it seals/unseals the kit bytes with **no** DB or filesystem
I/O — the caller decides what bytes to protect and where the file lives.

Wire shape (a single UTF-8 JSON object)::

    {
      "kit_version": 1,
      "kdf_suite": "scrypt-n16384-r8-p1",
      "aead_suite": "aesgcm-256",
      "instance_id": "<hex string>",
      "created_at": "<iso8601>",
      "seal_salt": "<b64url 32 random bytes>",
      "nonce": "<b64url 12 random bytes>",
      "ciphertext": "<b64url AES-256-GCM(payload)>"
    }

**Suite tags (PQ-forward by default).** Every cryptographic wire shape
carries a suite identifier so the algorithm can swap without breaking older
readers (see CLAUDE.md → "Crypto wire shapes carry a ``*_suite`` tag"). Here
``kdf_suite`` names the passphrase KDF and ``aead_suite`` the symmetric
cipher. Receivers validate against ``SUPPORTED_RECOVERY_*_SUITES`` and raise
:class:`UnsupportedRecoverySuite` on anything unknown — there is **no default
fallback**, which prevents a suite-downgrade attack.

**Header binding (AAD).** The clear header (everything except ``nonce`` and
``ciphertext``) is fed to AES-GCM as associated data, deterministically
serialized with sorted keys. Tampering any clear field — re-pointing the kit
at another ``instance_id``, back-dating ``created_at``, downgrading a suite —
changes the AAD and makes decryption fail. The kit can therefore neither be
transplanted nor silently downgraded.
"""

from __future__ import annotations

import logging
import os

import orjson
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from ..crypto import b64url_decode, b64url_encode

log = logging.getLogger(__name__)

RECOVERY_KIT_VERSION: int = 1

# scrypt parameters — also encoded in the suite string so a future bump ships
# a sibling constant rather than silently reinterpreting old kits.
SCRYPT_N: int = 2**14
SCRYPT_R: int = 8
SCRYPT_P: int = 1

RECOVERY_KDF_SUITE_SCRYPT: str = "scrypt-n16384-r8-p1"
RECOVERY_AEAD_SUITE_AESGCM: str = "aesgcm-256"
SUPPORTED_RECOVERY_KDF_SUITES: frozenset[str] = frozenset({RECOVERY_KDF_SUITE_SCRYPT})
SUPPORTED_RECOVERY_AEAD_SUITES: frozenset[str] = frozenset({RECOVERY_AEAD_SUITE_AESGCM})

_SALT_LEN = 32
_NONCE_LEN = 12
_KEY_LEN = 32

#: Clear-header fields, in addition to the suite/version/identity fields, that
#: must be present for a kit to parse. Used by both the header reader and the
#: unseal path.
_HEADER_FIELDS = (
    "kit_version",
    "kdf_suite",
    "aead_suite",
    "instance_id",
    "created_at",
    "seal_salt",
)


class UnsupportedRecoverySuite(ValueError):
    """Raised when a kit names a kdf_suite or aead_suite we don't support."""


class RecoveryKitError(ValueError):
    """Raised on a malformed kit, bad version, or auth failure (wrong
    passphrase / tampered bytes)."""


def _canonical_header_aad(header: dict) -> bytes:
    """Deterministic byte encoding of the clear header for use as AEAD AAD.

    Exactly the six clear fields, sorted-key JSON. Binds the ciphertext to
    the header so no field can be altered without breaking decryption.
    """
    clear = {k: header[k] for k in _HEADER_FIELDS}
    return orjson.dumps(clear, option=orjson.OPT_SORT_KEYS)


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=_KEY_LEN, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(passphrase.encode("utf-8"))


def seal_kit(
    payload: bytes, passphrase: str, *, instance_id: str, created_at: str
) -> bytes:
    """Seal ``payload`` into the full ``.shrk`` file bytes.

    ``created_at`` is an ISO-8601 string, passed through verbatim. The
    passphrase is stretched with scrypt; the payload is sealed with
    AES-256-GCM bound to the clear header as AAD.
    """
    seal_salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    key = _derive_key(passphrase, seal_salt)

    header = {
        "kit_version": RECOVERY_KIT_VERSION,
        "kdf_suite": RECOVERY_KDF_SUITE_SCRYPT,
        "aead_suite": RECOVERY_AEAD_SUITE_AESGCM,
        "instance_id": instance_id,
        "created_at": created_at,
        "seal_salt": b64url_encode(seal_salt),
    }
    aad = _canonical_header_aad(header)
    ciphertext = AESGCM(key).encrypt(nonce, payload, aad)

    kit = {
        **header,
        "nonce": b64url_encode(nonce),
        "ciphertext": b64url_encode(ciphertext),
    }
    return orjson.dumps(kit)


def _parse_and_validate_header(kit_bytes: bytes) -> dict:
    """Parse the JSON, check version + suites, return the full kit dict.

    Raises :class:`RecoveryKitError` on malformed input or bad version,
    :class:`UnsupportedRecoverySuite` on an unknown suite.
    """
    try:
        obj = orjson.loads(kit_bytes)
    except orjson.JSONDecodeError as exc:
        raise RecoveryKitError("recovery kit is not valid JSON") from exc
    if not isinstance(obj, dict):
        raise RecoveryKitError("recovery kit must be a JSON object")

    missing = [f for f in _HEADER_FIELDS if f not in obj]
    if missing:
        raise RecoveryKitError("recovery kit is missing required header fields")

    if obj["kit_version"] != RECOVERY_KIT_VERSION:
        raise RecoveryKitError("unsupported recovery kit version")

    # A non-string suite (JSON list/object) is not a supported suite — guard
    # the type first so the ``in frozenset`` check can't raise an unhandled
    # ``TypeError: unhashable type`` on untrusted file bytes (fail closed).
    if (
        not isinstance(obj["kdf_suite"], str)
        or obj["kdf_suite"] not in SUPPORTED_RECOVERY_KDF_SUITES
    ):
        raise UnsupportedRecoverySuite("unsupported recovery kit kdf_suite")
    if (
        not isinstance(obj["aead_suite"], str)
        or obj["aead_suite"] not in SUPPORTED_RECOVERY_AEAD_SUITES
    ):
        raise UnsupportedRecoverySuite("unsupported recovery kit aead_suite")

    return obj


def read_kit_header(kit_bytes: bytes) -> dict:
    """Parse + validate the CLEAR header WITHOUT the passphrase.

    For pre-decrypt display (which instance, when sealed, which suites).
    Returns the header dict. Raises :class:`RecoveryKitError` on a malformed
    kit or bad version, :class:`UnsupportedRecoverySuite` on an unknown suite.
    """
    obj = _parse_and_validate_header(kit_bytes)
    return {k: obj[k] for k in _HEADER_FIELDS}


def unseal_kit(kit_bytes: bytes, passphrase: str) -> tuple[dict, bytes]:
    """Return ``(header_dict, payload_bytes)``.

    Raises :class:`UnsupportedRecoverySuite` on an unknown suite,
    :class:`RecoveryKitError` on a malformed kit or auth failure (wrong
    passphrase / tampered bytes). The underlying crypto/decoding exception
    text is never leaked.
    """
    obj = _parse_and_validate_header(kit_bytes)
    try:
        seal_salt = b64url_decode(obj["seal_salt"])
        nonce = b64url_decode(obj["nonce"])
        ciphertext = b64url_decode(obj["ciphertext"])
    except (KeyError, ValueError, TypeError) as exc:
        raise RecoveryKitError("recovery kit has malformed binary fields") from exc

    aad = _canonical_header_aad(obj)
    key = _derive_key(passphrase, seal_salt)
    try:
        payload = AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise RecoveryKitError(
            "recovery kit authentication failed (wrong passphrase or tampered)"
        ) from exc
    except ValueError as exc:
        raise RecoveryKitError("recovery kit could not be decrypted") from exc

    header = {k: obj[k] for k in _HEADER_FIELDS}
    return header, payload
