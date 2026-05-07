"""Tests for :class:`PersonalCalendarInboundHandlers` (§23.60).

The inbound handler mirrors a remote organiser's invite into the
recipient's existing personal calendar with ``origin='remote_invite'``.
RSVP responses propagate back to the organiser's local row.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.calendar import Calendar, CalendarEvent
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.user import User
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.federation_inbound import (
    PersonalCalendarInboundHandlers,
)


class _Registry:
    def __init__(self) -> None:
        self.handlers: dict = {}

    def register(self, t, h):
        self.handlers[t] = h


class _FedSvc:
    def __init__(self) -> None:
        self._event_registry = _Registry()


class _FakeCalendarRepo:
    def __init__(self) -> None:
        self.calendars: dict[str, Calendar] = {}
        self.events: dict[str, CalendarEvent] = {}
        self.rsvps: dict = {}

    async def list_calendars_for_user(self, username):
        return [c for c in self.calendars.values() if c.owner_username == username]

    async def get_event(self, event_id):
        return self.events.get(event_id)

    async def get_event_by_remote(self, *, remote_instance_id, remote_event_id):
        for ev in self.events.values():
            if (
                ev.remote_instance_id == remote_instance_id
                and ev.remote_event_id == remote_event_id
            ):
                return ev
        return None

    async def save_event(self, event):
        self.events[event.id] = event
        return event

    async def delete_event(self, event_id):
        self.events.pop(event_id, None)

    async def upsert_rsvp(self, rsvp):
        self.rsvps[(rsvp.event_id, rsvp.user_id, rsvp.occurrence_at)] = rsvp

    async def remove_rsvp(self, event_id, user_id, *, occurrence_at=None):
        if occurrence_at is None:
            for k in [k for k in self.rsvps if k[0] == event_id and k[1] == user_id]:
                del self.rsvps[k]
        else:
            self.rsvps.pop((event_id, user_id, occurrence_at), None)


class _FakeUserRepo:
    def __init__(self) -> None:
        self.by_uid: dict[str, User] = {}

    async def get_by_user_id(self, user_id):
        return self.by_uid.get(user_id)


def _envelope(event_type, payload, from_instance="i_remote"):
    return FederationEvent(
        msg_id="m1",
        event_type=event_type,
        from_instance=from_instance,
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
    )


@pytest.fixture
def env():
    cal_repo = _FakeCalendarRepo()
    user_repo = _FakeUserRepo()
    bus = EventBus()
    handlers = PersonalCalendarInboundHandlers(
        bus=bus,
        calendar_repo=cal_repo,
        user_repo=user_repo,
    )
    fed = _FedSvc()
    handlers.attach_to(fed)

    # Recipient on this instance — has a personal calendar already.
    user_repo.by_uid["u-anna"] = User(
        username="anna",
        user_id="u-anna",
        display_name="Anna",
    )
    cal_repo.calendars["cal-anna"] = Calendar(
        id="cal-anna",
        name="Anna",
        color="#abcdef",
        owner_username="anna",
    )
    return fed, cal_repo, user_repo


async def test_inbound_invite_mirrors_into_recipient_calendar(env):
    fed, cal_repo, _ = env
    handler = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED
    ]
    now = datetime.now(timezone.utc)
    await handler(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED,
            {
                "event_id": "remote-evt-1",
                "summary": "BBQ at the Smiths'",
                "start": now.isoformat(),
                "end": (now + timedelta(hours=2)).isoformat(),
                "organizer_user_id": "u-bob-remote",
                "attendee_user_ids": ["u-anna"],
                "rsvp_enabled": True,
            },
        )
    )
    assert len(cal_repo.events) == 1
    ev = next(iter(cal_repo.events.values()))
    assert ev.origin == "remote_invite"
    assert ev.remote_event_id == "remote-evt-1"
    assert ev.remote_instance_id == "i_remote"
    assert ev.calendar_id == "cal-anna"
    assert ev.summary == "BBQ at the Smiths'"
    # First-receipt + rsvp_enabled → tentative auto-RSVP.
    assert any(r.status == "tentative" for r in cal_repo.rsvps.values())


async def test_inbound_invite_idempotent_under_redelivery(env):
    """Re-receiving the same envelope (network retry) collapses onto
    the same row instead of creating a duplicate."""
    fed, cal_repo, _ = env
    handler = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED
    ]
    now = datetime.now(timezone.utc)
    payload = {
        "event_id": "remote-evt-1",
        "summary": "BBQ",
        "start": now.isoformat(),
        "end": (now + timedelta(hours=2)).isoformat(),
        "organizer_user_id": "u-bob",
        "attendee_user_ids": ["u-anna"],
    }
    await handler(_envelope(
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED, payload
    ))
    await handler(_envelope(
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED, payload
    ))
    assert len(cal_repo.events) == 1


async def test_inbound_update_overwrites_in_place(env):
    fed, cal_repo, _ = env
    create = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED
    ]
    update = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_UPDATED
    ]
    now = datetime.now(timezone.utc)
    base = {
        "event_id": "remote-evt-1",
        "start": now.isoformat(),
        "end": (now + timedelta(hours=1)).isoformat(),
        "organizer_user_id": "u-bob",
        "attendee_user_ids": ["u-anna"],
    }
    await create(_envelope(
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED,
        {**base, "summary": "Original"},
    ))
    await update(_envelope(
        FederationEventType.PERSONAL_CALENDAR_EVENT_UPDATED,
        {**base, "summary": "Renamed"},
    ))
    assert len(cal_repo.events) == 1
    ev = next(iter(cal_repo.events.values()))
    assert ev.summary == "Renamed"


async def test_inbound_delete_removes_mirror(env):
    fed, cal_repo, _ = env
    create = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED
    ]
    delete = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_DELETED
    ]
    now = datetime.now(timezone.utc)
    await create(_envelope(
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED,
        {
            "event_id": "remote-evt-1",
            "summary": "BBQ",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "organizer_user_id": "u-bob",
            "attendee_user_ids": ["u-anna"],
        },
    ))
    await delete(_envelope(
        FederationEventType.PERSONAL_CALENDAR_EVENT_DELETED,
        {"event_id": "remote-evt-1"},
    ))
    assert cal_repo.events == {}


async def test_inbound_rsvp_writes_to_local_event(env):
    """An RSVP coming back from a paired peer lands on the organiser's
    local event id (not a mirror) — verifies the responder echoes the
    organiser's id back."""
    fed, cal_repo, _ = env
    rsvp = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED
    ]
    now = datetime.now(timezone.utc)
    cal_repo.events["local-evt"] = CalendarEvent(
        id="local-evt",
        calendar_id="cal-anna",
        summary="Picnic",
        start=now,
        end=now + timedelta(hours=1),
        created_by="u-anna",
    )
    await rsvp(_envelope(
        FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED,
        {
            "event_id": "local-evt",
            "user_id": "u-bob-remote",
            "status": "accepted",
            "occurrence_at": now.isoformat(),
            "updated_at": now.isoformat(),
        },
    ))
    assert any(
        r.user_id == "u-bob-remote" and r.status == "accepted"
        for r in cal_repo.rsvps.values()
    )


async def test_inbound_rsvp_for_unknown_event_dropped(env):
    """A peer sending an RSVP for an event we don't own shouldn't
    create an RSVP row — would otherwise let an attacker write
    phantom RSVPs."""
    fed, cal_repo, _ = env
    rsvp = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED
    ]
    now = datetime.now(timezone.utc)
    await rsvp(_envelope(
        FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED,
        {
            "event_id": "ghost-evt",
            "user_id": "u-bob",
            "status": "accepted",
            "occurrence_at": now.isoformat(),
        },
    ))
    assert cal_repo.rsvps == {}
