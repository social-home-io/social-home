"""TaskFederationOutbound — per-event SPACE_TASK_* fan-out."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.domain.events import TaskCreated, TaskDeleted, TaskUpdated
from socialhome.domain.federation import FederationEventType
from socialhome.domain.task import Task, TaskStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.task_federation_outbound import (
    TaskFederationOutbound,
)


class _FakeFed:
    """Records `broadcast_to_space_members` calls.

    F2a switched task outbound from per-peer `send_event` to mesh-
    routed broadcast so members behind a relay receive task mutations
    too. The broadcast helper handles the member-list lookup + the
    per-peer `send_with_mesh_fallback` internally.
    """

    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, FederationEventType, dict]] = []

    async def broadcast_to_space_members(
        self,
        space_id,
        event_type,
        payload,
        **kwargs,
    ):
        self.broadcasts.append((space_id, event_type, payload))


def _task(tid: str) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=tid,
        list_id="L",
        title=f"t{tid}",
        status=TaskStatus.TODO,
        position=0,
        created_by="u",
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def env():
    bus = EventBus()
    fed = _FakeFed()
    out = TaskFederationOutbound(bus=bus, federation_service=fed)
    out.wire()
    return bus, fed


async def test_household_task_is_not_federated(env):
    bus, fed = env
    await bus.publish(TaskCreated(task=_task("t1"), space_id=None))
    assert fed.broadcasts == []


async def test_space_task_created_broadcasts_with_payload(env):
    bus, fed = env
    await bus.publish(TaskCreated(task=_task("t1"), space_id="sp-A"))
    assert len(fed.broadcasts) == 1
    space_id, event_type, payload = fed.broadcasts[0]
    assert space_id == "sp-A"
    assert event_type is FederationEventType.SPACE_TASK_CREATED
    assert payload["id"] == "t1"
    assert payload["space_id"] == "sp-A"
    assert payload["status"] == "todo"


async def test_space_task_deleted_minimal_payload(env):
    bus, fed = env
    await bus.publish(
        TaskDeleted(task_id="t1", list_id="L", space_id="sp-A"),
    )
    assert len(fed.broadcasts) == 1
    space_id, event_type, payload = fed.broadcasts[0]
    assert space_id == "sp-A"
    assert event_type is FederationEventType.SPACE_TASK_DELETED
    assert payload == {"id": "t1", "list_id": "L", "space_id": "sp-A"}


async def test_space_task_updated_event_type(env):
    bus, fed = env
    await bus.publish(TaskUpdated(task=_task("t1"), space_id="sp-B"))
    assert len(fed.broadcasts) == 1
    space_id, event_type, _payload = fed.broadcasts[0]
    assert space_id == "sp-B"
    assert event_type is FederationEventType.SPACE_TASK_UPDATED
