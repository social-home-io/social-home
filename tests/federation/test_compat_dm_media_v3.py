"""Compat tree: DM_MESSAGE media-payload v_3 → v_2 fallback.

Issue #319 paragraph 5 picks ``fallback`` for the DM-media bump:
sub-v_3 peers receive a synthesised ``type='text'`` message naming
the file the sender intended to share, rather than dropping the
send entirely. This module verifies that contract by hand-rolling
payloads through ``compat.transform_for_peer`` at known
``peer_version`` values.
"""

from __future__ import annotations

from socialhome.domain.federation import FederationEventType
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.federation import compat


def _media_payload(**overrides):
    base = {
        "conversation_id": "c1",
        "message_id": "m1",
        "sender_user_id": "u-alice",
        "sender_display_name": "Alice",
        "type": "image",
        "content": "",
        "media_url": "media/abc.webp",
        "file_name": "cat.jpg",
        "mime_type": "image/jpeg",
        "file_size_bytes": 1_234_567,
        "media_blob_id": "blob-1",
        "reply_to_id": None,
        "occurred_at": "2026-05-15T08:00:00+00:00",
        "recipient_user_ids": ["u-bob"],
    }
    base.update(overrides)
    return base


def test_passes_through_for_v3_peer() -> None:
    """A v_3 peer gets the canonical payload untouched."""
    out = compat.transform_for_peer(
        event_type=FederationEventType.DM_MESSAGE,
        payload=_media_payload(),
        peer_version=FederationCapability.MIN_FOR_DM_MEDIA_SYNC,
    )
    assert out is not None
    assert out["type"] == "image"
    assert out["media_url"] == "media/abc.webp"
    assert out["file_name"] == "cat.jpg"
    assert out["media_blob_id"] == "blob-1"


def test_passes_through_for_above_v3_peer() -> None:
    """A future v_4 peer also gets the canonical v_3 payload."""
    out = compat.transform_for_peer(
        event_type=FederationEventType.DM_MESSAGE,
        payload=_media_payload(),
        peer_version=99,
    )
    assert out is not None
    assert out["type"] == "image"


def test_text_message_passes_through_at_any_version() -> None:
    """Non-media DM messages are shape-compatible with v_1 already."""
    payload = _media_payload(
        type="text",
        content="hello",
        media_url=None,
        file_name=None,
        mime_type=None,
        file_size_bytes=None,
        media_blob_id=None,
    )
    out = compat.transform_for_peer(
        event_type=FederationEventType.DM_MESSAGE,
        payload=payload,
        peer_version=2,
    )
    assert out is not None
    assert out["type"] == "text"
    assert out["content"] == "hello"


def test_v2_peer_gets_text_fallback_for_image() -> None:
    """The §319 paragraph 5 ``fallback`` policy in action.

    A v_2 receiver doesn't know how to render a media type and
    doesn't expect the ``DM_MEDIA_BLOB`` follow-up, so the
    ``dm_media_v3`` shim rewrites the envelope to a regular text
    message naming the file the sender intended to share.
    """
    out = compat.transform_for_peer(
        event_type=FederationEventType.DM_MESSAGE,
        payload=_media_payload(type="image", file_name="vacation.jpg"),
        peer_version=2,
    )
    assert out is not None
    assert out["type"] == "text"
    # User-facing copy carries the file name + a reason.
    assert "vacation.jpg" in out["content"]
    assert "update" in out["content"].lower()
    # Every v_3 media field is stripped — the v_2 receiver sees a
    # clean text-shape envelope.
    for key in (
        "media_url",
        "file_name",
        "mime_type",
        "file_size_bytes",
        "media_blob_id",
        "preview_bytes_b64",
    ):
        assert key not in out, f"{key} leaked into the v_2 fallback shape"


def test_v2_peer_gets_text_fallback_for_video() -> None:
    out = compat.transform_for_peer(
        event_type=FederationEventType.DM_MESSAGE,
        payload=_media_payload(type="video", file_name="dance.mp4"),
        peer_version=2,
    )
    assert out is not None
    assert out["type"] == "text"
    assert "dance.mp4" in out["content"]


def test_v2_peer_gets_text_fallback_for_file() -> None:
    out = compat.transform_for_peer(
        event_type=FederationEventType.DM_MESSAGE,
        payload=_media_payload(type="file", file_name="invoice.pdf"),
        peer_version=2,
    )
    assert out is not None
    assert out["type"] == "text"
    assert "invoice.pdf" in out["content"]


def test_fallback_carries_unrelated_routing_fields_through() -> None:
    """The shim only rewrites the user-visible content fields.

    Routing fields (``conversation_id``, ``message_id``,
    ``sender_user_id``, etc.) and v_2-compatible features
    (``reply_to_id``) flow through unchanged so the receiver can
    still thread the message + dedupe by message_id.
    """
    out = compat.transform_for_peer(
        event_type=FederationEventType.DM_MESSAGE,
        payload=_media_payload(
            type="image",
            reply_to_id="m-prev",
        ),
        peer_version=2,
    )
    assert out is not None
    assert out["conversation_id"] == "c1"
    assert out["message_id"] == "m1"
    assert out["sender_user_id"] == "u-alice"
    assert out["reply_to_id"] == "m-prev"


def test_no_transforms_registered_for_other_events_pass_through() -> None:
    """The compat tree is opt-in per event-type.

    Events without a registered transform should round-trip
    untouched at every peer_version.
    """
    payload = {"event": "INSTANCE_CAPABILITIES_UPDATED", "proto_version": 99}
    out = compat.transform_for_peer(
        event_type=FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
        payload=payload,
        peer_version=1,
    )
    assert out == payload
