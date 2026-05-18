"""CommentFederationOutbound — fan space-scoped comment mutations to peer instances.

Household-scoped comments (``space_id is None``) must stay local; the
service fans out to the space's member instances only, dropping its
own id and any empty ids defensively.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.domain.events import (
    CommentAdded,
    CommentDeleted,
    CommentUpdated,
)
from socialhome.domain.federation import FederationEventType
from socialhome.domain.post import Comment, CommentType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.comment_federation_outbound import (
    CommentFederationOutbound,
)


class _FakeFederationService:
    def __init__(self, own_instance_id: str = "own-inst") -> None:
        self._own_instance_id = own_instance_id
        self.sent: list[tuple[str, FederationEventType, dict, str | None]] = []

    async def send_event(
        self,
        *,
        to_instance_id,
        event_type,
        payload,
        space_id=None,
    ):
        self.sent.append((to_instance_id, event_type, payload, space_id))
        return None


class _FakeSpaceRepo:
    def __init__(self, members: dict[str, list[str]]) -> None:
        self._members = members

    async def list_member_instances(self, space_id: str) -> list[str]:
        return list(self._members.get(space_id, []))


def _comment(**over) -> Comment:
    base = dict(
        id="c1",
        post_id="p1",
        author="u1",
        type=CommentType.TEXT,
        created_at=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        content="hi",
        parent_id=None,
    )
    base.update(over)
    return Comment(**base)


@pytest.fixture
def env():
    bus = EventBus()
    fed = _FakeFederationService()
    repo = _FakeSpaceRepo(
        {
            "sp-A": ["own-inst", "peer-1", "peer-2"],
            "sp-B": ["peer-3"],
        }
    )
    out = CommentFederationOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=repo,
    )
    out.wire()
    return bus, fed


async def test_household_comment_is_not_federated(env):
    bus, fed = env
    await bus.publish(CommentAdded(post_id="p1", comment=_comment(), space_id=None))
    assert fed.sent == []


async def test_space_comment_added_fanouts_excluding_self(env):
    bus, fed = env
    await bus.publish(CommentAdded(post_id="p1", comment=_comment(), space_id="sp-A"))
    recipients = [r[0] for r in fed.sent]
    assert recipients == ["peer-1", "peer-2"]
    for _to, event_type, payload, space_id in fed.sent:
        assert event_type is FederationEventType.SPACE_COMMENT_CREATED
        assert space_id == "sp-A"
        assert payload["id"] == "c1"
        assert payload["post_id"] == "p1"
        assert payload["space_id"] == "sp-A"
        assert payload["created_at"] == "2026-05-18T10:00:00+00:00"


async def test_space_comment_updated_uses_update_event_type(env):
    bus, fed = env
    edited = _comment(
        content="edited",
        edited_at=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc),
    )
    await bus.publish(CommentUpdated(post_id="p1", comment=edited, space_id="sp-B"))
    assert [r[1] for r in fed.sent] == [FederationEventType.SPACE_COMMENT_UPDATED]
    assert fed.sent[0][0] == "peer-3"
    payload = fed.sent[0][2]
    assert payload == {
        "id": "c1",
        "post_id": "p1",
        "content": "edited",
        "edited_at": "2026-05-18T11:00:00+00:00",
        "space_id": "sp-B",
    }


async def test_space_comment_deleted_minimal_payload(env):
    bus, fed = env
    await bus.publish(
        CommentDeleted(post_id="p1", comment_id="c1", space_id="sp-A"),
    )
    assert len(fed.sent) == 2
    for _to, event_type, payload, space_id in fed.sent:
        assert event_type is FederationEventType.SPACE_COMMENT_DELETED
        assert payload == {"id": "c1", "post_id": "p1", "space_id": "sp-A"}
        assert space_id == "sp-A"


async def test_household_comment_updated_or_deleted_is_not_federated(env):
    bus, fed = env
    await bus.publish(
        CommentUpdated(post_id="p1", comment=_comment(), space_id=None),
    )
    await bus.publish(
        CommentDeleted(post_id="p1", comment_id="c1", space_id=None),
    )
    assert fed.sent == []


async def test_empty_instance_id_is_skipped(env):
    """Defensive: a peer with an empty/missing id in the member list
    must not produce a phantom send."""
    bus = EventBus()
    fed = _FakeFederationService()
    repo = _FakeSpaceRepo({"sp-A": ["", "peer-1", "own-inst"]})
    out = CommentFederationOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=repo,
    )
    out.wire()
    await bus.publish(CommentAdded(post_id="p1", comment=_comment(), space_id="sp-A"))
    assert [r[0] for r in fed.sent] == ["peer-1"]
