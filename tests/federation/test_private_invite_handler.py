"""Inbound coverage for :class:`PrivateSpaceInviteHandler`.

Complements :mod:`test_private_invite_zero_leak` (outbound) by
exercising every inbound event type: invite received, accept, decline,
member removed, plus the missing-field skip branches.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from socialhome.domain.events import (
    RemoteSpaceInviteAccepted,
    RemoteSpaceInviteDeclined,
    RemoteSpaceInviteReceived,
    RemoteSpaceMemberRemoved,
)
from socialhome.federation.private_invite_handler import PrivateSpaceInviteHandler
from socialhome.infrastructure.event_bus import EventBus


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, event) -> None:
        self.events.append(event)


def _event(event_type: str, payload: dict, *, from_instance: str = "peer-1"):
    # FederationEvent is a dataclass, but we only use .payload and
    # .from_instance attrs — a namespace is cheaper and clearer.
    return SimpleNamespace(
        event_type=event_type,
        payload=payload,
        from_instance=from_instance,
    )


@pytest.fixture
def handler():
    bus = _RecordingBus()
    space_repo = AsyncMock()
    space_repo.save_remote_invitation = AsyncMock()
    space_repo.get_invitation_by_token = AsyncMock()
    space_repo.update_invitation_status = AsyncMock()
    remote_members = AsyncMock()
    remote_members.add = AsyncMock()
    remote_members.remove = AsyncMock()
    cover_repo = AsyncMock()
    cover_repo.set = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=bus,  # type: ignore[arg-type]
        space_repo=space_repo,
        remote_member_repo=remote_members,
        cover_repo=cover_repo,
    )
    return SimpleNamespace(
        h=h,
        bus=bus,
        space_repo=space_repo,
        remote_members=remote_members,
        cover_repo=cover_repo,
    )


async def test_invite_happy_path(handler):
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        {
            "space_id": "sp1",
            "invite_token": "tkn",
            "invitee_user_id": "u1",
            "inviter_user_id": "u2",
            "space_display_hint": "Board",
        },
    )
    await handler.h._on_invite(ev)
    handler.space_repo.save_remote_invitation.assert_awaited_once()
    assert any(isinstance(e, RemoteSpaceInviteReceived) for e in handler.bus.events)


async def test_invite_missing_fields_noops(handler):
    ev = _event("SPACE_PRIVATE_INVITE", {})
    await handler.h._on_invite(ev)
    handler.space_repo.save_remote_invitation.assert_not_awaited()
    assert handler.bus.events == []


async def test_accept_no_token_noops(handler):
    ev = _event("SPACE_PRIVATE_INVITE_ACCEPT", {})
    await handler.h._on_accept(ev)
    handler.remote_members.add.assert_not_awaited()
    assert handler.bus.events == []


async def test_accept_unknown_token_noops(handler):
    handler.space_repo.get_invitation_by_token.return_value = None
    ev = _event("SPACE_PRIVATE_INVITE_ACCEPT", {"invite_token": "x"})
    await handler.h._on_accept(ev)
    handler.remote_members.add.assert_not_awaited()


async def test_accept_happy_path(handler):
    handler.space_repo.get_invitation_by_token.return_value = {
        "id": 42,
        "space_id": "sp-a",
    }
    ev = _event(
        "SPACE_PRIVATE_INVITE_ACCEPT",
        {
            "invite_token": "abc",
            "invitee_user_id": "u1",
            "invitee_public_key": "pk",
            "invitee_display_name": "Bob",
        },
    )
    await handler.h._on_accept(ev)
    handler.remote_members.add.assert_awaited_once()
    handler.space_repo.update_invitation_status.assert_awaited_with(42, "accepted")
    assert any(isinstance(e, RemoteSpaceInviteAccepted) for e in handler.bus.events)


async def test_decline_no_token_noops(handler):
    ev = _event("SPACE_PRIVATE_INVITE_DECLINE", {})
    await handler.h._on_decline(ev)
    handler.space_repo.update_invitation_status.assert_not_awaited()


async def test_decline_unknown_token_noops(handler):
    handler.space_repo.get_invitation_by_token.return_value = None
    ev = _event("SPACE_PRIVATE_INVITE_DECLINE", {"invite_token": "nope"})
    await handler.h._on_decline(ev)
    handler.space_repo.update_invitation_status.assert_not_awaited()


async def test_decline_happy_path(handler):
    handler.space_repo.get_invitation_by_token.return_value = {
        "id": 5,
        "space_id": "sp-b",
    }
    ev = _event(
        "SPACE_PRIVATE_INVITE_DECLINE",
        {"invite_token": "tk", "invitee_user_id": "u1"},
    )
    await handler.h._on_decline(ev)
    handler.space_repo.update_invitation_status.assert_awaited_with(5, "declined")
    assert any(isinstance(e, RemoteSpaceInviteDeclined) for e in handler.bus.events)


async def test_member_removed_missing_fields_noops(handler):
    ev = _event("SPACE_REMOTE_MEMBER_REMOVED", {})
    await handler.h._on_member_removed(ev)
    handler.remote_members.remove.assert_not_awaited()


async def test_member_removed_happy_path(handler):
    ev = _event(
        "SPACE_REMOTE_MEMBER_REMOVED",
        {"space_id": "sp-c", "user_id": "u-bye"},
    )
    await handler.h._on_member_removed(ev)
    handler.remote_members.remove.assert_awaited_once_with(
        "sp-c",
        "peer-1",
        "u-bye",
    )
    assert any(isinstance(e, RemoteSpaceMemberRemoved) for e in handler.bus.events)


async def test_invite_with_roster_seats_remote_members_for_each_peer(handler):
    """#115 — the ``space_meta`` blob carries a ``roster`` of every
    member already in the space. The joiner's instance writes each
    of them into ``space_remote_members`` so her local Members tab
    shows the full household-spanning roster (not just herself).
    The invitee's *own* user_id is skipped — that comes via
    ``space_members`` when she accepts."""
    handler.remote_members.add = AsyncMock()
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        {
            "space_id": "sp-remote",
            "invite_token": "tkn",
            "invitee_user_id": "u-self",
            "inviter_user_id": "u-pascal",
            "space_meta": {
                "name": "Family",
                "owner_instance_id": "peer-1",
                "owner_username": "pascal",
                "identity_public_key": "abc",
                "roster": [
                    {
                        "user_id": "u-pascal",
                        "instance_id": "peer-1",
                        "display_name": "Pascal",
                        "role": "owner",
                    },
                    {
                        "user_id": "u-anna",
                        "instance_id": "peer-1",
                        "display_name": "Anna",
                        "role": "member",
                    },
                    {
                        "user_id": "u-self",
                        "instance_id": "peer-2",
                        "display_name": "Me",
                        "role": "member",
                    },
                ],
            },
        },
    )
    await handler.h._on_invite(ev)
    # Two roster entries (Pascal + Anna) get seated as remote
    # members; the invitee's own row is skipped.
    assert handler.remote_members.add.await_count == 2
    seated_ids = {
        call.kwargs["user_id"] for call in handler.remote_members.add.await_args_list
    }
    assert seated_ids == {"u-pascal", "u-anna"}


async def test_invite_with_cover_bytes_writes_to_cover_repo(handler):
    """#116 — when ``space_meta`` carries ``cover_webp_base64`` we
    persist the bytes via the cover repo so the stub renders the
    host's real cover image instead of the gradient placeholder.
    Without ``cover_hash`` the write is skipped (defensive)."""
    import base64

    fake_webp = b"RIFF\x00\x00\x00\x00WEBPVP8L"
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        {
            "space_id": "sp-cover",
            "invite_token": "tkn",
            "invitee_user_id": "u-self",
            "inviter_user_id": "u-pascal",
            "space_meta": {
                "name": "Family",
                "owner_instance_id": "peer-1",
                "owner_username": "pascal",
                "identity_public_key": "abc",
                "cover_hash": "deadbeef",
                "cover_webp_base64": base64.b64encode(fake_webp).decode("ascii"),
            },
        },
    )
    await handler.h._on_invite(ev)
    handler.cover_repo.set.assert_awaited_once()
    call = handler.cover_repo.set.call_args
    assert call.args[0] == "sp-cover"
    assert call.kwargs["bytes_webp"] == fake_webp
    assert call.kwargs["hash"] == "deadbeef"


async def test_invite_skips_cover_write_when_no_bytes(handler):
    """Older senders that don't ship cover bytes leave the cover
    repo untouched. The stub still gets seated; the SPA falls back
    to the gradient placeholder."""
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        {
            "space_id": "sp-no-cover",
            "invite_token": "tkn",
            "invitee_user_id": "u-self",
            "inviter_user_id": "u-pascal",
            "space_meta": {
                "name": "Family",
                "owner_instance_id": "peer-1",
                "owner_username": "pascal",
                "identity_public_key": "abc",
                "cover_hash": "deadbeef",
                # no cover_webp_base64 — cover repo stays untouched.
            },
        },
    )
    await handler.h._on_invite(ev)
    handler.cover_repo.set.assert_not_awaited()


async def test_invite_with_space_meta_seats_local_stub(handler):
    """B2: inbound SPACE_PRIVATE_INVITE carrying ``space_meta`` seats a
    local stub row so accept can immediately insert the joiner's
    ``space_members`` row pointing at it."""
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        {
            "space_id": "sp-remote",
            "invite_token": "tkn",
            "invitee_user_id": "u-self",
            "inviter_user_id": "u-pascal",
            "space_display_hint": "Family",
            "space_meta": {
                "name": "Family",
                "emoji": "🏡",
                "owner_instance_id": "peer-1",
                "owner_username": "pascal",
                "identity_public_key": "abc123",
                "config_sequence": 1,
                "space_type": "private",
                "join_mode": "invite_only",
                "features": {"calendar": True, "gallery": True},
                "tz": "Europe/Berlin",
            },
        },
    )
    await handler.h._on_invite(ev)
    handler.space_repo.save.assert_awaited_once()
    saved = handler.space_repo.save.call_args.args[0]
    assert saved.id == "sp-remote"
    assert saved.name == "Family"
    assert saved.emoji == "🏡"
    assert saved.owner_instance_id == "peer-1"
    assert saved.owner_username == "pascal"
    assert saved.features.calendar is True
    assert saved.features.gallery is True


async def test_invite_without_space_meta_skips_stub_creation(handler):
    """Pre-B2 senders ship no ``space_meta``; we MUST stay quiet
    rather than synthesise garbage into the stub. The receiver still
    sees the invitation banner — only the local-stub seat is skipped
    until the issuer upgrades."""
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        {
            "space_id": "sp1",
            "invite_token": "tkn",
            "invitee_user_id": "u-self",
            "inviter_user_id": "u-pascal",
            "space_display_hint": "Family",
        },
    )
    await handler.h._on_invite(ev)
    handler.space_repo.save.assert_not_awaited()


async def test_member_removed_clears_local_stub_when_last_member_leaves(handler):
    """B2: when the host kicks the joiner from a remote space and
    they were the only local ``space_members`` row, we mark the stub
    dissolved so the SPA stops listing it. Locally-owned spaces
    (different owner_instance_id) are NEVER dissolved on a remote
    SPACE_REMOTE_MEMBER_REMOVED."""
    handler.space_repo.delete_member = AsyncMock()
    handler.space_repo.list_members = AsyncMock(return_value=[])
    stub_space = SimpleNamespace(owner_instance_id="peer-1")
    handler.space_repo.get = AsyncMock(return_value=stub_space)
    handler.space_repo.mark_dissolved = AsyncMock()
    ev = _event(
        "SPACE_REMOTE_MEMBER_REMOVED",
        {"space_id": "sp-remote", "user_id": "u-self"},
    )
    await handler.h._on_member_removed(ev)
    handler.space_repo.delete_member.assert_awaited_once_with(
        "sp-remote",
        "u-self",
    )
    handler.space_repo.mark_dissolved.assert_awaited_once_with("sp-remote")


async def test_member_removed_keeps_locally_owned_space(handler):
    """If the row we keep for this space_id is *ours* (owner_instance_id
    matches our own), the SPACE_REMOTE_MEMBER_REMOVED event is for one
    of OUR remote members on someone else's instance. We must NOT
    dissolve our own space row in that case."""
    handler.space_repo.delete_member = AsyncMock()
    handler.space_repo.list_members = AsyncMock(return_value=[])
    own_space = SimpleNamespace(owner_instance_id="us")
    handler.space_repo.get = AsyncMock(return_value=own_space)
    handler.space_repo.mark_dissolved = AsyncMock()
    ev = _event(
        "SPACE_REMOTE_MEMBER_REMOVED",
        {"space_id": "sp-ours", "user_id": "u-they"},
        from_instance="peer-1",
    )
    await handler.h._on_member_removed(ev)
    handler.space_repo.mark_dissolved.assert_not_awaited()


async def test_attach_to_registers_four_handlers():
    """`attach_to` wires the four event-type → handler bindings."""
    bus = EventBus()
    space_repo = AsyncMock()
    remote_members = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=bus,  # type: ignore[arg-type]
        space_repo=space_repo,
        remote_member_repo=remote_members,
    )

    class _FakeRegistry:
        def __init__(self) -> None:
            self.bindings: dict = {}

        def register(self, event_type, handler):
            self.bindings[event_type] = handler

    class _FakeFedSvc:
        def __init__(self) -> None:
            self._event_registry = _FakeRegistry()

    fed = _FakeFedSvc()
    h.attach_to(fed)  # type: ignore[arg-type]
    assert len(fed._event_registry.bindings) == 4
