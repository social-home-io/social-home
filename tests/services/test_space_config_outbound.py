"""SpaceConfigOutbound — broadcast SPACE_CONFIG_CHANGED on every config edit.

Before this service the bus event fired locally but never reached remote
member stubs in realtime — only the §D1b catch-up reply
(``_push_config_to``) ever shipped the federation event. Toggling
``location_mode`` on the host left every remote stub with the prior
mode and silently broke the space map (the API's strict
``mode_filter`` filtered out otherwise-valid pin rows).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.domain.events import SpaceConfigChanged
from socialhome.domain.federation import FederationEventType
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.space_config_outbound import SpaceConfigOutbound


def _make_space(
    *,
    space_id: str = "sp-1",
    owner_instance_id: str = "inst-self",
    location_mode: str = "gps",
    config_sequence: int = 5,
    name: str = "Living Room",
) -> Space:
    return Space(
        id=space_id,
        name=name,
        owner_instance_id=owner_instance_id,
        owner_username="alice",
        identity_public_key="aa" * 32,
        config_sequence=config_sequence,
        features=SpaceFeatures(location=True, location_mode=location_mode),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )


@pytest.fixture
def fed():
    f = MagicMock()
    f.broadcast_to_space_members = AsyncMock()
    f._own_instance_id = "inst-self"
    return f


@pytest.fixture
def space_repo():
    r = MagicMock()
    r.get = AsyncMock(return_value=_make_space())
    return r


async def test_config_changed_broadcasts_post_mutation_snapshot(
    fed,
    space_repo,
):
    """The bus event carries the changed-fields dict; the broadcast
    must carry the FULL post-mutation snapshot so remote stubs can
    apply via ``stub_space_from_metadata``."""
    bus = EventBus()
    SpaceConfigOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=space_repo,
    ).wire()

    await bus.publish(
        SpaceConfigChanged(
            space_id="sp-1",
            event_type="feature_changed",
            payload={"features": {"location_mode": "gps"}},
            sequence=5,
        ),
    )

    fed.broadcast_to_space_members.assert_awaited_once()
    call = fed.broadcast_to_space_members.await_args
    assert call.args[0] == "sp-1"
    assert call.args[1] == FederationEventType.SPACE_CONFIG_CHANGED
    payload = call.args[2]
    assert payload["space_id"] == "sp-1"
    assert payload["sequence"] == 5
    assert payload["event_type"] == "feature_changed"
    # Flat legacy fields ship for pre-§D1b consumers.
    assert payload["name"] == "Living Room"
    assert payload["join_mode"] == "invite_only"
    # space_meta carries the modern shape the inbound handler reads.
    assert payload["space_meta"]["name"] == "Living Room"
    assert payload["space_meta"]["features"]["location_mode"] == "gps"


async def test_config_changed_skipped_when_not_owner(fed):
    """Only the owner host broadcasts — a remote-stub holder must
    not re-broadcast somebody else's space config."""
    bus = EventBus()
    remote_owned = _make_space(owner_instance_id="inst-remote")
    space_repo = MagicMock()
    space_repo.get = AsyncMock(return_value=remote_owned)
    SpaceConfigOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=space_repo,
    ).wire()
    await bus.publish(
        SpaceConfigChanged(
            space_id="sp-1",
            event_type="rename",
            payload={"name": "Renamed"},
            sequence=2,
        ),
    )
    fed.broadcast_to_space_members.assert_not_awaited()


async def test_config_changed_skipped_when_space_missing(fed):
    bus = EventBus()
    space_repo = MagicMock()
    space_repo.get = AsyncMock(return_value=None)
    SpaceConfigOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=space_repo,
    ).wire()
    await bus.publish(
        SpaceConfigChanged(
            space_id="sp-gone",
            event_type="rename",
            payload={"name": "Nope"},
            sequence=1,
        ),
    )
    fed.broadcast_to_space_members.assert_not_awaited()


async def test_config_changed_broadcast_failure_is_swallowed(space_repo):
    bus = EventBus()
    fed = MagicMock()
    fed._own_instance_id = "inst-self"
    fed.broadcast_to_space_members = AsyncMock(
        side_effect=RuntimeError("transport down"),
    )
    SpaceConfigOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=space_repo,
    ).wire()
    # Must not raise.
    await bus.publish(
        SpaceConfigChanged(
            space_id="sp-1",
            event_type="rename",
            payload={"name": "X"},
            sequence=1,
        ),
    )
