"""Outbound story federation — :class:`StoryFederationOutbound`.

Covers:
* First frame → ``STORY_CREATED``; subsequent frames → ``STORY_FRAME_APPENDED``.
* ``audience_kind = 'all_paired'`` fans to every paired peer.
* ``audience_kind = 'households'`` fans to the listed instance_ids only.
* ``audience_kind = 'users'`` resolves user_ids → home instances.
* Echo-loop guard: events whose author lives on a peer instance never
  re-fan to peers (so inbound republished events stay local).
* ``StoryFrameRemoved`` / ``StoryRemoved`` map to the matching FET.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.domain.events import StoryFrameAdded, StoryFrameRemoved, StoryRemoved
from socialhome.domain.federation import FederationEventType, RemoteInstance
from socialhome.services.story_federation_outbound import StoryFederationOutbound


def _frame_event(
    *,
    author: str = "uid-author",
    is_first: bool = True,
    audience_kind: str = "all_paired",
    audience: tuple[str, ...] = (),
) -> StoryFrameAdded:
    return StoryFrameAdded(
        story_id="s-1",
        frame_id="f-1",
        author_user_id=author,
        story_date="2026-05-05",
        sequence=1,
        is_first_frame=is_first,
        audience_kind=audience_kind,
        audience=audience,
        frame_type="image",
        media_url="/api/media/x.webp",
        caption_text=None,
        caption_emoji=None,
        duration_ms=None,
        expires_at="2026-06-04T00:00:00Z",
    )


@pytest.fixture
def stack():
    """Wire a StoryFederationOutbound against fully mocked deps."""
    federation = MagicMock()
    federation.own_instance_id = "self"
    federation.send_event = AsyncMock()
    federation_repo = MagicMock()
    user_repo = MagicMock()
    bus = MagicMock()  # not used in unit tests; .wire() is exercised separately
    out = StoryFederationOutbound(
        bus=bus,
        federation_service=federation,
        federation_repo=federation_repo,
        user_repo=user_repo,
    )
    return out, federation, federation_repo, user_repo


def _peer(iid: str) -> RemoteInstance:
    inst = MagicMock(spec=RemoteInstance)
    inst.id = iid
    return inst


async def test_first_frame_fans_story_created_to_all_paired(stack):
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_instances = AsyncMock(
        return_value=[_peer("peer-a"), _peer("peer-b")],
    )
    await out._on_frame_added(_frame_event(is_first=True))
    sent = [c.kwargs for c in fed.send_event.call_args_list]
    assert {c["to_instance_id"] for c in sent} == {"peer-a", "peer-b"}
    assert all(c["event_type"] is FederationEventType.STORY_CREATED for c in sent)


async def test_subsequent_frame_uses_frame_appended(stack):
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_instances = AsyncMock(return_value=[_peer("peer-a")])
    await out._on_frame_added(_frame_event(is_first=False))
    assert fed.send_event.call_args.kwargs["event_type"] is (
        FederationEventType.STORY_FRAME_APPENDED
    )


async def test_remote_author_skips_echo_loop(stack):
    """When the author lives on a peer, the same DomainEvent must not re-fan."""
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="peer-source")
    fed_repo.list_instances = AsyncMock(return_value=[_peer("peer-a")])
    await out._on_frame_added(_frame_event())
    assert fed.send_event.call_count == 0


async def test_users_audience_resolves_each_user_to_home_instance(stack):
    out, fed, fed_repo, user_repo = stack

    # Author is local; audience = two specific users on two peers.
    async def _home(uid):
        return {
            "uid-author": "self",
            "uid-bob": "peer-bob",
            "uid-carol": "peer-carol",
        }.get(uid)

    user_repo.get_instance_for_user = AsyncMock(side_effect=_home)
    fed_repo.list_instances = AsyncMock(return_value=[])
    await out._on_frame_added(
        _frame_event(audience_kind="users", audience=("uid-bob", "uid-carol")),
    )
    sent = {c.kwargs["to_instance_id"] for c in fed.send_event.call_args_list}
    assert sent == {"peer-bob", "peer-carol"}


async def test_households_audience_uses_listed_instance_ids(stack):
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_instances = AsyncMock(return_value=[])
    await out._on_frame_added(
        _frame_event(
            audience_kind="households",
            audience=("peer-a", "peer-b", "self"),  # own filtered out
        ),
    )
    sent = {c.kwargs["to_instance_id"] for c in fed.send_event.call_args_list}
    assert sent == {"peer-a", "peer-b"}


async def test_frame_removed_maps_to_frame_deleted(stack):
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_instances = AsyncMock(return_value=[_peer("peer-a")])
    await out._on_frame_removed(
        StoryFrameRemoved(
            story_id="s-1",
            frame_id="f-1",
            author_user_id="uid-author",
            audience_kind="all_paired",
            audience=(),
        ),
    )
    assert fed.send_event.call_args.kwargs["event_type"] is (
        FederationEventType.STORY_FRAME_DELETED
    )


async def test_story_removed_maps_to_story_deleted(stack):
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_instances = AsyncMock(return_value=[_peer("peer-a")])
    await out._on_story_removed(
        StoryRemoved(
            story_id="s-1",
            author_user_id="uid-author",
            audience_kind="all_paired",
            audience=(),
        ),
    )
    assert fed.send_event.call_args.kwargs["event_type"] is (
        FederationEventType.STORY_DELETED
    )


async def test_own_instance_filtered_from_paired_peers(stack):
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_instances = AsyncMock(
        return_value=[_peer("self"), _peer("peer-a")],
    )
    await out._on_frame_added(_frame_event())
    assert fed.send_event.call_count == 1
    assert fed.send_event.call_args.kwargs["to_instance_id"] == "peer-a"


async def test_send_failure_does_not_abort_other_peers(stack):
    """A bad peer doesn't stop fan-out to the rest of the audience."""
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_instances = AsyncMock(
        return_value=[_peer("peer-bad"), _peer("peer-ok")],
    )

    async def _maybe_fail(**kwargs):
        if kwargs["to_instance_id"] == "peer-bad":
            raise RuntimeError("peer down")

    fed.send_event.side_effect = _maybe_fail
    await out._on_frame_added(_frame_event())
    sent = [c.kwargs["to_instance_id"] for c in fed.send_event.call_args_list]
    assert sent == ["peer-bad", "peer-ok"] or sent == ["peer-ok", "peer-bad"]
