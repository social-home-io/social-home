"""Tests for the binary app-channel framing (``fed-app-v1``)."""

from __future__ import annotations

import struct

import pytest

from socialhome.federation import app_framing as af


def test_round_trip_header_and_payload():
    header = b'{"msg_id":"abc","event_type":"app_message"}'
    payload = b"\x00\x01\x02chess-move\xff\xfe"
    frame = af.encode(header, payload)
    decoded = af.decode(frame)
    assert decoded.frame_type == af.FRAME_TYPE_APP_MSG
    assert decoded.header == header
    assert decoded.payload == payload


def test_round_trip_empty_payload():
    """Control frames may carry no payload."""
    frame = af.encode(b"hdr", b"")
    decoded = af.decode(frame)
    assert decoded.header == b"hdr"
    assert decoded.payload == b""


def test_round_trip_preserves_arbitrary_bytes():
    """Payload must survive bytes that look like the length prefix."""
    header = b"h"
    payload = struct.pack(">I", 999999) + b"\x00\x00\x00\x00more"
    decoded = af.decode(af.encode(header, payload))
    assert decoded.payload == payload


def test_custom_frame_type_preserved():
    frame = af.encode(b"h", b"p", frame_type=7)
    assert af.decode(frame).frame_type == 7


def test_encode_rejects_out_of_range_frame_type():
    with pytest.raises(af.AppFramingError):
        af.encode(b"h", b"p", frame_type=256)


def test_encode_rejects_oversize_header():
    with pytest.raises(af.AppFramingError):
        af.encode(b"x" * (af.MAX_HEADER_BYTES + 1), b"")


def test_encode_rejects_oversize_payload():
    with pytest.raises(af.AppFramingError):
        af.encode(b"h", b"x" * (af.MAX_PAYLOAD_BYTES + 1))


def test_decode_rejects_truncated_prefix():
    with pytest.raises(af.AppFramingError):
        af.decode(b"\x01\x00\x00")  # < 5 bytes


def test_decode_rejects_truncated_payload():
    frame = af.encode(b"hdr", b"payload")
    # Lop off the last few payload bytes — declared len now overshoots.
    with pytest.raises(af.AppFramingError):
        af.decode(frame[:-3])


def test_decode_rejects_declared_header_too_large():
    # frame_type=1, header_len = MAX+1, no body present.
    buf = struct.pack(">BI", 1, af.MAX_HEADER_BYTES + 1)
    with pytest.raises(af.AppFramingError):
        af.decode(buf)


def test_decode_rejects_declared_payload_too_large():
    # Valid header, but payload_len overshoots the ceiling.
    header = b"h"
    buf = (
        struct.pack(">BI", 1, len(header))
        + header
        + struct.pack(">I", af.MAX_PAYLOAD_BYTES + 1)
    )
    with pytest.raises(af.AppFramingError):
        af.decode(buf)


def test_iter_complete_frames_extracts_multiple():
    f1 = af.encode(b"h1", b"p1")
    f2 = af.encode(b"h2", b"p2longer")
    frames, leftover = af.iter_complete_frames(f1 + f2)
    assert leftover == b""
    assert [f.header for f in frames] == [b"h1", b"h2"]
    assert [f.payload for f in frames] == [b"p1", b"p2longer"]


def test_iter_complete_frames_returns_partial_tail():
    """A frame split across SCTP messages leaves the tail for next read."""
    f1 = af.encode(b"h1", b"p1")
    f2 = af.encode(b"h2", b"p2")
    buf = f1 + f2[:4]  # f2 only partially present
    frames, leftover = af.iter_complete_frames(buf)
    assert [f.header for f in frames] == [b"h1"]
    assert leftover == f2[:4]
    # Feeding the rest completes f2.
    frames2, leftover2 = af.iter_complete_frames(leftover + f2[4:])
    assert leftover2 == b""
    assert [f.header for f in frames2] == [b"h2"]


def test_iter_complete_frames_empty_buffer():
    frames, leftover = af.iter_complete_frames(b"")
    assert frames == []
    assert leftover == b""


def test_iter_complete_frames_trailing_incomplete_prefix():
    """A buffer ending with fewer than 5 bytes is returned as leftover."""
    f1 = af.encode(b"h1", b"p1")
    partial_prefix = b"\x01\x00\x00"  # only 3 of the 5-byte prefix
    frames, leftover = af.iter_complete_frames(f1 + partial_prefix)
    assert [f.header for f in frames] == [b"h1"]
    assert leftover == partial_prefix


def test_supported_suite_constants():
    assert af.APP_AEAD_SUITE_AESGCM_256 in af.SUPPORTED_APP_AEAD_SUITES
    assert af.CHANNEL_LABEL == "fed-app-v1"
