"""Binary framing for the federation app DataChannel (``fed-app-v1``).

The federation PeerConnection carries DataChannels (§24.12):

* ``fed-v1`` — small, latency-sensitive control + routine envelopes,
  serialised as UTF-8 JSON (``federation/transport.py``).
* ``fed-media-v1`` — bulk media chunks (DM + space blobs) as binary frames
  (``federation/media_framing.py``).
* ``fed-app-v1`` — small app-to-app messages (e.g. chess moves, shared
  whiteboard ops, custom mini-app payloads) as **binary frames**, so high-
  frequency app traffic doesn't head-of-line-block control envelopes on
  ``fed-v1`` and doesn't pay the ~37 % base64 tax of JSON transport. This
  is the *fast path*; a JSON :data:`APP_MESSAGE` federation event on
  ``fed-v1`` serves as fallback for peers whose capabilities haven't
  advertised ``fed-app-v1`` yet.

Each frame on the wire is::

    [u8 frame_type][u32 header_len BE][header_bytes][u32 payload_len BE][payload_bytes]

* ``frame_type`` discriminates frame kinds so the *same* versioned channel
  can grow new frame shapes without a new channel label or a protocol-version
  bump. v1 ships only :data:`FRAME_TYPE_APP_MSG`; an older receiver skips an
  unknown type rather than erroring (forward-compatible).
* ``header_bytes`` is **opaque** to this module — the app transport puts the
  signed federation envelope JSON here verbatim, so the exact bytes that were
  signed are the exact bytes the §24.11 pipeline re-parses. This module never
  parses or re-serialises the header (that would risk a signed-bytes mismatch).
* ``payload_bytes`` is the AES-256-GCM-sealed app message
  (``nonce || ciphertext``). MAY be empty for control frames.

App messages are intentionally small (chess move, whiteboard delta, game
state patch) — the payload cap of 1 MiB (:data:`MAX_PAYLOAD_BYTES`) is
generous for those use cases while still rejecting accidental bulk uploads
that belong on ``fed-media-v1``.

Reassembly: SCTP preserves message boundaries, so one ``channel.send``
normally arrives as one whole frame. :func:`iter_complete_frames`
nonetheless tolerates a peer that coalesces or splits messages — it
extracts every complete frame from a running buffer and returns the
unconsumed tail.

This is a deliberately separate module from
:mod:`socialhome.federation.media_framing`: media frames have a larger
payload ceiling (4 MiB) suited to blob chunks, whereas app frames cap at
1 MiB to keep the fast path honest. Both share the same wire layout and
``_frame_span`` pattern so the two are easy to audit together.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# ─── Wire constants ──────────────────────────────────────────────────────

#: DataChannel label for the binary app-message stream. Versioned — a
#: breaking frame-format change bumps to ``fed-app-v2`` rather than
#: silently reinterpreting bytes.
CHANNEL_LABEL: str = "fed-app-v1"

#: Frame-type tags. Additive: a new tag is forward-compatible because
#: receivers skip unknown types (see :func:`iter_complete_frames`).
FRAME_TYPE_APP_MSG: int = 1

#: Symmetric AEAD suite for the sealed app-message payload. Carried inside
#: the signed+encrypted envelope metadata (never in cleartext) and
#: validated against :data:`SUPPORTED_APP_AEAD_SUITES` on receipt —
#: per the CLAUDE.md crypto-suite rule, unknown suites are rejected with
#: no default fallback. AES-256 is Grover-resistant (~128-bit PQ); the
#: PQ lever for app messages is the key-delivery channel, not this primitive.
APP_AEAD_SUITE_AESGCM_256: str = "aesgcm-256"
SUPPORTED_APP_AEAD_SUITES: frozenset[str] = frozenset(
    {APP_AEAD_SUITE_AESGCM_256},
)

#: Upper bounds to cap allocation from a malicious/garbled peer. A
#: federation envelope is a few KiB; an app message (chess move, whiteboard
#: delta, game-state patch) is comfortably under 1 MiB. The 1 MiB payload
#: ceiling is deliberately tighter than the media channel's 4 MiB — it
#: keeps the fast path honest (bulk blobs go to ``fed-media-v1``) and caps
#: worst-case allocation from a misbehaving peer at a quarter of the media
#: channel's ceiling.
MAX_HEADER_BYTES: int = (
    1 << 20
)  # 1 MiB — matches media_framing; envelopes are KiB-range
MAX_PAYLOAD_BYTES: int = (
    1 << 20
)  # 1 MiB — smaller than media (4 MiB); app msgs are small

_PREFIX = struct.Struct(">BI")  # frame_type (u8) + header_len (u32 BE)
_U32 = struct.Struct(">I")


class AppFramingError(Exception):
    """Raised when wire bytes don't match the frame spec.

    Oversized declared lengths from a malicious or garbled peer are the
    primary trigger — the receiver rejects rather than allocating arbitrarily
    large buffers. Mirrors :class:`~socialhome.federation.media_framing.MediaFramingError`.
    """

    __slots__ = ()


class UnsupportedAppAeadSuite(ValueError):
    """Raised when an app message declares an AEAD suite we don't support.

    Mirrors the ``Unsupported*Suite`` pattern used across the federation
    crypto surfaces (signatures, KEM, key delivery, media). Receivers reject —
    never fall back to a default.
    """

    __slots__ = ()


@dataclass(slots=True, frozen=True)
class AppFrame:
    """One decoded wire frame."""

    frame_type: int
    header: bytes
    payload: bytes


# ─── Encode ──────────────────────────────────────────────────────────────


def encode(
    header: bytes,
    payload: bytes = b"",
    *,
    frame_type: int = FRAME_TYPE_APP_MSG,
) -> bytes:
    """Encode one wire frame.

    ``header`` and ``payload`` are bytes-like; ``payload`` may be ``b""``
    for control frames. Raises :class:`AppFramingError` if either
    exceeds its size ceiling or ``frame_type`` is out of the u8 range.
    """
    if not 0 <= frame_type <= 0xFF:
        raise AppFramingError(f"frame_type out of range: {frame_type}")
    h = bytes(header)
    p = bytes(payload)
    if len(h) > MAX_HEADER_BYTES:
        raise AppFramingError(f"header too large: {len(h)} > {MAX_HEADER_BYTES}")
    if len(p) > MAX_PAYLOAD_BYTES:
        raise AppFramingError(f"payload too large: {len(p)} > {MAX_PAYLOAD_BYTES}")
    return _PREFIX.pack(frame_type, len(h)) + h + _U32.pack(len(p)) + p


# ─── Decode ──────────────────────────────────────────────────────────────


def _frame_span(buf: bytes, off: int) -> tuple[int, int, int, int] | None:
    """Return ``(header_off, header_len, payload_off, payload_len)`` for the
    frame starting at ``off``, or ``None`` if ``buf`` doesn't yet hold a
    complete frame. Raises :class:`AppFramingError` on a structurally
    invalid (oversized) declared length.
    """
    n = len(buf)
    if n - off < _PREFIX.size:
        return None
    _ftype, header_len = _PREFIX.unpack_from(buf, off)
    if header_len > MAX_HEADER_BYTES:
        raise AppFramingError(f"declared header_len too large: {header_len}")
    header_off = off + _PREFIX.size
    plen_off = header_off + header_len
    if n < plen_off + _U32.size:
        return None
    (payload_len,) = _U32.unpack_from(buf, plen_off)
    if payload_len > MAX_PAYLOAD_BYTES:
        raise AppFramingError(f"declared payload_len too large: {payload_len}")
    payload_off = plen_off + _U32.size
    if n < payload_off + payload_len:
        return None
    return header_off, header_len, payload_off, payload_len


def decode(buf: bytes) -> AppFrame:
    """Decode exactly one frame from ``buf`` (which must hold ≥ one frame).

    Trailing bytes after the first frame are ignored — use
    :func:`iter_complete_frames` for streaming reassembly.
    """
    span = _frame_span(buf, 0)
    if span is None:
        raise AppFramingError("buffer too short for a complete frame")
    header_off, header_len, payload_off, payload_len = span
    frame_type = buf[0]
    return AppFrame(
        frame_type=frame_type,
        header=bytes(buf[header_off : header_off + header_len]),
        payload=bytes(buf[payload_off : payload_off + payload_len]),
    )


def iter_complete_frames(buf: bytes) -> tuple[list[AppFrame], bytes]:
    """Extract every complete frame from ``buf``.

    Returns ``(frames, leftover)`` where ``leftover`` is the unconsumed
    tail (a partially-received frame) the caller should prepend to the
    next read. Raises :class:`AppFramingError` only on a structurally
    invalid declared length — a merely-incomplete tail is returned as
    ``leftover``, not an error.
    """
    frames: list[AppFrame] = []
    off = 0
    while off < len(buf):
        span = _frame_span(buf, off)
        if span is None:
            break
        header_off, header_len, payload_off, payload_len = span
        frames.append(
            AppFrame(
                frame_type=buf[off],
                header=bytes(buf[header_off : header_off + header_len]),
                payload=bytes(buf[payload_off : payload_off + payload_len]),
            )
        )
        off = payload_off + payload_len
    return frames, bytes(buf[off:])
