"""Outbound moment federation — :class:`MomentFederationOutbound`."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.domain.events import (
    MomentCreated,
    MomentDeleted,
    MomentReactionChanged,
)
from socialhome.domain.federation import FederationEventType, RemoteInstance
from socialhome.services.moment_federation_outbound import (
    MomentFederationOutbound,
)


def _peer(iid: str) -> RemoteInstance:
    inst = MagicMock(spec=RemoteInstance)
    inst.id = iid
    return inst


def _create_event(
    *,
    author: str = "uid-author",
    parent: str | None = None,
) -> MomentCreated:
    return MomentCreated(
        moment_id="m-1",
        author_user_id=author,
        content="hello",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=parent,
        parent_author_user_id=None,
        origin_instance_id="self",
        expires_at="2026-06-01T00:00:00+00:00",
    )


@pytest.fixture
def stack():
    federation = MagicMock()
    federation.own_instance_id = "self"
    federation.send_event = AsyncMock()
    federation_repo = MagicMock()
    user_repo = MagicMock()
    bus = MagicMock()
    out = MomentFederationOutbound(
        bus=bus,
        federation_service=federation,
        federation_repo=federation_repo,
        user_repo=user_repo,
    )
    return out, federation, federation_repo, user_repo


# ── Origin fan-out (bus path) ────────────────────────────────────────────


async def test_create_fans_to_all_paired_with_hop_1(stack):
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_instances = AsyncMock(
        return_value=[_peer("peer-a"), _peer("peer-b")],
    )
    await out._on_created(_create_event())
    sent = [c.kwargs for c in fed.send_event.call_args_list]
    assert {c["to_instance_id"] for c in sent} == {"peer-a", "peer-b"}
    assert all(c["event_type"] is FederationEventType.MOMENT_CREATED for c in sent)
    assert all(c["payload"]["hop_count"] == 1 for c in sent)


async def test_remote_author_skips_origin_fan(stack):
    """Inbound republished MomentCreated must not re-fan from the bus."""
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="peer-source")
    fed_repo.list_instances = AsyncMock(return_value=[_peer("peer-a")])
    await out._on_created(_create_event())
    fed.send_event.assert_not_called()


async def test_delete_fans_with_hop_1(stack):
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_instances = AsyncMock(return_value=[_peer("peer-a")])
    await out._on_deleted(
        MomentDeleted(
            moment_id="m-1",
            author_user_id="uid-author",
            origin_instance_id="self",
        )
    )
    kwargs = fed.send_event.call_args.kwargs
    assert kwargs["event_type"] is FederationEventType.MOMENT_DELETED
    assert kwargs["payload"]["hop_count"] == 1


async def test_reaction_set_unicasts_to_author_home(stack):
    out, fed, _fed_repo, user_repo = stack

    async def _home(uid):
        return {
            "uid-reactor": "self",
            "uid-author": "peer-author",
        }.get(uid)

    user_repo.get_instance_for_user = AsyncMock(side_effect=_home)
    await out._on_reaction_changed(
        MomentReactionChanged(
            moment_id="m-1",
            reactor_user_id="uid-reactor",
            author_user_id="uid-author",
            emoji="🔥",
        )
    )
    fed.send_event.assert_awaited_once()
    kwargs = fed.send_event.call_args.kwargs
    assert kwargs["to_instance_id"] == "peer-author"
    assert kwargs["event_type"] is FederationEventType.MOMENT_REACTED


async def test_reaction_cleared_uses_reaction_removed(stack):
    out, fed, _fed_repo, user_repo = stack

    async def _home(uid):
        return {"uid-reactor": "self", "uid-author": "peer-x"}.get(uid)

    user_repo.get_instance_for_user = AsyncMock(side_effect=_home)
    await out._on_reaction_changed(
        MomentReactionChanged(
            moment_id="m-1",
            reactor_user_id="uid-reactor",
            author_user_id="uid-author",
            emoji=None,
        )
    )
    kwargs = fed.send_event.call_args.kwargs
    assert kwargs["event_type"] is FederationEventType.MOMENT_REACTION_REMOVED


async def test_reaction_skipped_when_author_local(stack):
    """Author + reactor both local → no federation."""
    out, fed, _fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    await out._on_reaction_changed(
        MomentReactionChanged(
            moment_id="m-1",
            reactor_user_id="uid-reactor",
            author_user_id="uid-author",
            emoji="👍",
        )
    )
    fed.send_event.assert_not_called()


# ── Relay (3-hop) ────────────────────────────────────────────────────────


async def test_relay_inbound_bumps_hop_and_excludes_origin_and_sender(stack):
    out, fed, fed_repo, _user_repo = stack
    fed_repo.list_instances = AsyncMock(
        return_value=[
            _peer("peer-origin"),
            _peer("peer-sender"),
            _peer("peer-onward"),
        ],
    )
    payload = {
        "moment_id": "m-1",
        "author_user_id": "uid-author",
        "origin_instance_id": "peer-origin",
        "hop_count": 1,
    }
    await out.relay_inbound(
        event_type=FederationEventType.MOMENT_CREATED,
        payload=payload,
        from_instance="peer-sender",
    )
    sent = [c.kwargs for c in fed.send_event.call_args_list]
    assert {c["to_instance_id"] for c in sent} == {"peer-onward"}
    assert all(c["payload"]["hop_count"] == 2 for c in sent)


async def test_relay_inbound_stops_at_max_hops(stack):
    out, fed, fed_repo, _user_repo = stack
    fed_repo.list_instances = AsyncMock(return_value=[_peer("peer-onward")])
    payload = {
        "moment_id": "m-1",
        "author_user_id": "uid-author",
        "origin_instance_id": "peer-origin",
        "hop_count": 3,  # already at the cap
    }
    await out.relay_inbound(
        event_type=FederationEventType.MOMENT_CREATED,
        payload=payload,
        from_instance="peer-sender",
    )
    fed.send_event.assert_not_called()


async def test_relay_inbound_skips_with_invalid_hop(stack):
    out, fed, fed_repo, _user_repo = stack
    fed_repo.list_instances = AsyncMock(return_value=[_peer("peer-onward")])
    await out.relay_inbound(
        event_type=FederationEventType.MOMENT_CREATED,
        payload={"hop_count": "weird", "origin_instance_id": "peer-origin"},
        from_instance="peer-sender",
    )
    fed.send_event.assert_not_called()


async def test_send_failure_swallowed_per_peer(stack):
    """A misbehaving peer doesn't break the relay loop."""
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_instances = AsyncMock(
        return_value=[_peer("peer-bad"), _peer("peer-ok")],
    )

    async def _maybe_fail(**kwargs):
        if kwargs["to_instance_id"] == "peer-bad":
            raise RuntimeError("peer down")

    fed.send_event.side_effect = _maybe_fail
    # Should NOT raise — should still reach peer-ok.
    await out._on_created(_create_event())
    sent = {c.kwargs["to_instance_id"] for c in fed.send_event.call_args_list}
    assert "peer-ok" in sent


async def test_list_instances_failure_returns_no_peers(stack):
    """A failing federation_repo.list_instances logs and returns []."""
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_instances = AsyncMock(side_effect=RuntimeError("db down"))
    await out._on_created(_create_event())
    fed.send_event.assert_not_called()
