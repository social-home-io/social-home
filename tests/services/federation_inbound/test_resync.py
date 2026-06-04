"""Tests for :class:`ResyncInboundHandlers` (§319.6 INSTANCE_RESYNC_REQUEST).

The handler dispatches a peer's resync request by scope:

* ``"capabilities"`` → re-advertise our ``proto_version``.
* ``"space:<id>"`` → replay the space's content (membership-gated).
* ``"calendar:<id>"`` → replay the space's calendar (membership-gated).

Unknown / empty scopes (and ``space:`` / ``calendar:`` with an empty id)
drop silently.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from socialhome.domain.federation import FederationEventType
from socialhome.services.federation_inbound.resync import (
    EPOCH,
    ResyncInboundHandlers,
)


class _FakeCapabilitiesOutbound:
    def __init__(self) -> None:
        self.resends: list[str] = []

    async def resend_to(self, instance_id: str) -> bool:
        self.resends.append(instance_id)
        return True


class _FakeResume:
    def __init__(self) -> None:
        self.space_calls: list[dict] = []
        self.calendar_calls: list[dict] = []

    async def replay_space_to(self, *, space_id, instance_id, since) -> int:
        self.space_calls.append(
            {"space_id": space_id, "instance_id": instance_id, "since": since}
        )
        return 0

    async def replay_calendar_to(self, *, space_id, instance_id, since) -> int:
        self.calendar_calls.append(
            {"space_id": space_id, "instance_id": instance_id, "since": since}
        )
        return 0


def _event(scope, *, requester: str = "peer-a"):
    return SimpleNamespace(
        event_type=FederationEventType.INSTANCE_RESYNC_REQUEST,
        from_instance=requester,
        space_id=None,
        payload={} if scope is None else {"scope": scope},
    )


def _handlers():
    caps = _FakeCapabilitiesOutbound()
    resume = _FakeResume()
    handlers = ResyncInboundHandlers(
        capabilities_outbound=caps,
        space_resume=resume,
    )
    return handlers, caps, resume


@pytest.mark.asyncio
async def test_capabilities_scope_re_advertises():
    handlers, caps, resume = _handlers()
    await handlers._on_resync_request(_event("capabilities"))
    assert caps.resends == ["peer-a"]
    assert resume.space_calls == []
    assert resume.calendar_calls == []


@pytest.mark.asyncio
async def test_space_scope_replays_space():
    handlers, caps, resume = _handlers()
    await handlers._on_resync_request(_event("space:S1"))
    assert resume.space_calls == [
        {"space_id": "S1", "instance_id": "peer-a", "since": EPOCH}
    ]
    assert caps.resends == []
    assert resume.calendar_calls == []


@pytest.mark.asyncio
async def test_calendar_scope_replays_calendar():
    handlers, caps, resume = _handlers()
    await handlers._on_resync_request(_event("calendar:S1"))
    assert resume.calendar_calls == [
        {"space_id": "S1", "instance_id": "peer-a", "since": EPOCH}
    ]
    assert caps.resends == []
    assert resume.space_calls == []


@pytest.mark.asyncio
async def test_unknown_scope_drops():
    handlers, caps, resume = _handlers()
    await handlers._on_resync_request(_event("nonsense"))
    assert caps.resends == []
    assert resume.space_calls == []
    assert resume.calendar_calls == []


@pytest.mark.asyncio
async def test_empty_scope_drops():
    handlers, caps, resume = _handlers()
    await handlers._on_resync_request(_event(None))
    assert caps.resends == []
    assert resume.space_calls == []
    assert resume.calendar_calls == []


@pytest.mark.asyncio
async def test_space_scope_with_empty_id_drops():
    handlers, caps, resume = _handlers()
    await handlers._on_resync_request(_event("space:"))
    assert resume.space_calls == []
    assert caps.resends == []


@pytest.mark.asyncio
async def test_calendar_scope_with_empty_id_drops():
    handlers, caps, resume = _handlers()
    await handlers._on_resync_request(_event("calendar:"))
    assert resume.calendar_calls == []
    assert caps.resends == []


@pytest.mark.asyncio
async def test_attach_to_registers_on_event_registry():
    handlers, _, _ = _handlers()
    registered: dict = {}
    registry = SimpleNamespace(
        register=lambda event_type, fn: registered.__setitem__(event_type, fn)
    )
    fed = SimpleNamespace(_event_registry=registry)
    handlers.attach_to(fed)
    assert (
        registered[FederationEventType.INSTANCE_RESYNC_REQUEST]
        == handlers._on_resync_request
    )
