"""Tests for the binary media-channel framing (``fed-media-v1``)."""

from __future__ import annotations

import struct

import pytest

from socialhome.federation import media_framing as mf


def test_round_trip_header_and_payload():
    header = b'{"msg_id":"abc","event_type":"dm_media_blob"}'
    payload = b"\x00\x01\x02binary\xff\xfe"
    frame = mf.encode(header, payload)
    decoded = mf.decode(frame)
    assert decoded.frame_type == mf.FRAME_TYPE_MEDIA_CHUNK
    assert decoded.header == header
    assert decoded.payload == payload


def test_round_trip_empty_payload():
    """Control frames may carry no payload."""
    frame = mf.encode(b"hdr", b"")
    decoded = mf.decode(frame)
    assert decoded.header == b"hdr"
    assert decoded.payload == b""


def test_round_trip_preserves_arbitrary_bytes():
    """Payload must survive bytes that look like the length prefix."""
    header = b"h"
    payload = struct.pack(">I", 999999) + b"\x00\x00\x00\x00more"
    decoded = mf.decode(mf.encode(header, payload))
    assert decoded.payload == payload


def test_custom_frame_type_preserved():
    frame = mf.encode(b"h", b"p", frame_type=7)
    assert mf.decode(frame).frame_type == 7


def test_encode_rejects_out_of_range_frame_type():
    with pytest.raises(mf.MediaFramingError):
        mf.encode(b"h", b"p", frame_type=256)


def test_encode_rejects_oversize_header():
    with pytest.raises(mf.MediaFramingError):
        mf.encode(b"x" * (mf.MAX_HEADER_BYTES + 1), b"")


def test_encode_rejects_oversize_payload():
    with pytest.raises(mf.MediaFramingError):
        mf.encode(b"h", b"x" * (mf.MAX_PAYLOAD_BYTES + 1))


def test_decode_rejects_truncated_prefix():
    with pytest.raises(mf.MediaFramingError):
        mf.decode(b"\x01\x00\x00")  # < 5 bytes


def test_decode_rejects_truncated_payload():
    frame = mf.encode(b"hdr", b"payload")
    # Lop off the last few payload bytes — declared len now overshoots.
    with pytest.raises(mf.MediaFramingError):
        mf.decode(frame[:-3])


def test_decode_rejects_declared_header_too_large():
    # frame_type=1, header_len = MAX+1, no body present.
    buf = struct.pack(">BI", 1, mf.MAX_HEADER_BYTES + 1)
    with pytest.raises(mf.MediaFramingError):
        mf.decode(buf)


def test_decode_rejects_declared_payload_too_large():
    # Valid header, but payload_len overshoots the ceiling.
    header = b"h"
    buf = (
        struct.pack(">BI", 1, len(header))
        + header
        + struct.pack(">I", mf.MAX_PAYLOAD_BYTES + 1)
    )
    with pytest.raises(mf.MediaFramingError):
        mf.decode(buf)


def test_iter_complete_frames_extracts_multiple():
    f1 = mf.encode(b"h1", b"p1")
    f2 = mf.encode(b"h2", b"p2longer")
    frames, leftover = mf.iter_complete_frames(f1 + f2)
    assert leftover == b""
    assert [f.header for f in frames] == [b"h1", b"h2"]
    assert [f.payload for f in frames] == [b"p1", b"p2longer"]


def test_iter_complete_frames_returns_partial_tail():
    """A frame split across SCTP messages leaves the tail for next read."""
    f1 = mf.encode(b"h1", b"p1")
    f2 = mf.encode(b"h2", b"p2")
    buf = f1 + f2[:4]  # f2 only partially present
    frames, leftover = mf.iter_complete_frames(buf)
    assert [f.header for f in frames] == [b"h1"]
    assert leftover == f2[:4]
    # Feeding the rest completes f2.
    frames2, leftover2 = mf.iter_complete_frames(leftover + f2[4:])
    assert leftover2 == b""
    assert [f.header for f in frames2] == [b"h2"]


def test_iter_complete_frames_empty_buffer():
    frames, leftover = mf.iter_complete_frames(b"")
    assert frames == []
    assert leftover == b""


def test_supported_suite_constants():
    assert mf.MEDIA_AEAD_SUITE_AESGCM_256 in mf.SUPPORTED_MEDIA_AEAD_SUITES
    assert mf.CHANNEL_LABEL == "fed-media-v1"
