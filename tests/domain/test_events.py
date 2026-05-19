"""Tests for socialhome.domain.events."""

from __future__ import annotations

from datetime import datetime, timezone

from socialhome.domain.events import DomainEvent, PeerTransportChanged, PostCreated
from socialhome.domain.post import Post, PostType


def test_post_created_is_domain_event():
    """PostCreated is a DomainEvent with a valid occurred_at timestamp."""
    now = datetime.now(timezone.utc)
    p = Post(id="p1", author="u1", type=PostType.TEXT, created_at=now)
    e = PostCreated(post=p)
    assert isinstance(e, DomainEvent)
    assert e.occurred_at is not None


def test_peer_transport_changed_shape():
    """PeerTransportChanged has instance_id and transport fields."""
    e = PeerTransportChanged(instance_id="iid-1", transport="rtc")
    assert e.instance_id == "iid-1"
    assert e.transport == "rtc"

    e2 = PeerTransportChanged(instance_id="iid-2", transport="https")
    assert e2.transport == "https"
