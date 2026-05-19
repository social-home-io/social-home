"""Tests for LocalHomeLocationUpdated → LOCAL_HOME_LOCATION_CHANGED fan-out.

Covers two branches:
  1. Confirmed peer at proto_version >= 5 receives the envelope.
  2. Confirmed peer at proto_version < 5 is silently skipped.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.domain.events import LocalHomeLocationUpdated
from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.federation_service import FederationService
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager


# ─── Shared helpers ──────────────────────────────────────────────────────────


def _make_kek() -> KeyManager:
    import os

    return KeyManager(os.urandom(32))


def _peer(instance_id: str, *, proto_version: int) -> RemoteInstance:
    return RemoteInstance(
        id=instance_id,
        display_name=instance_id,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="https://example.test/inbox",
        local_inbox_id="local-" + instance_id,
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
        proto_version=proto_version,
    )


class _InMemoryFederationRepo:
    """Minimal in-memory repo for home-location fan-out tests."""

    def __init__(self) -> None:
        self._instances: dict[str, RemoteInstance] = {}

    async def save_instance(self, inst: RemoteInstance) -> RemoteInstance:
        self._instances[inst.id] = inst
        return inst

    async def get_instance(self, instance_id: str) -> RemoteInstance | None:
        return self._instances.get(instance_id)

    async def list_instances(
        self,
        *,
        source: str | None = None,
        status: str | None = None,
    ) -> list[RemoteInstance]:
        result = list(self._instances.values())
        if status is not None:
            result = [i for i in result if i.status.value == status]
        return result

    # Stub the rest of the AbstractFederationRepo surface that
    # FederationService touches at construction time.
    async def get_instance_by_local_inbox_id(self, local_inbox_id):
        return None

    async def mark_reachable(self, instance_id: str) -> None:
        pass

    async def mark_unreachable(self, instance_id: str) -> None:
        pass

    async def load_replay_cache(self, within_hours: int = 1):
        return []

    async def insert_replay_id(self, msg_id: str) -> None:
        pass

    async def prune_replay_cache(self, cutoff_iso: str) -> int:
        return 0

    async def create_pairing(self, session) -> None:
        pass

    async def get_pairing(self, token: str):
        return None

    async def update_pairing(self, session) -> None:
        pass

    async def delete_pairing(self, token: str) -> None:
        pass

    async def ban_instance_from_space(self, space_id, instance_id, *, reason=None):
        pass

    async def is_instance_banned_from_space(self, space_id, instance_id) -> bool:
        return False

    async def get_local_identity(self):
        return None

    async def delete_instance(self, instance_id: str) -> None:
        self._instances.pop(instance_id, None)

    async def update_inbox(self, instance_id: str, new_url: str) -> None:
        pass

    async def list_instances_in_space(self, space_id: str):
        return []


class _InMemoryOutboxRepo:
    async def enqueue(self, *, instance_id, event_type, payload_json, **kw):
        return "stub-id"

    async def list_due(self, limit=50):
        return []

    async def mark_delivered(self, entry_id: str) -> None:
        pass

    async def mark_failed(self, entry_id: str) -> None:
        pass

    async def reschedule(self, entry_id, next_attempt_at, attempts) -> None:
        pass

    async def expire_past_retention(self, now_iso: str) -> int:
        return 0

    async def count_pending_for(self, instance_id: str) -> int:
        return 0


def _make_service() -> tuple[FederationService, _InMemoryFederationRepo, EventBus]:
    own_kp = generate_identity_keypair()
    own_id = derive_instance_id(own_kp.public_key)
    fed_repo = _InMemoryFederationRepo()
    bus = EventBus()
    svc = FederationService(
        db=MagicMock(),
        federation_repo=fed_repo,
        outbox_repo=_InMemoryOutboxRepo(),
        key_manager=_make_kek(),
        bus=bus,
        own_instance_id=own_id,
        own_identity_seed=own_kp.private_key,
        own_identity_pk=own_kp.public_key,
    )
    return svc, fed_repo, bus


# ─── Tests ───────────────────────────────────────────────────────────────────


_SEND_EVENT_PATH = (
    "socialhome.federation.federation_service.FederationService.send_event"
)


@pytest.mark.asyncio
async def test_local_home_location_fans_out_to_v5_confirmed_peer():
    """LocalHomeLocationUpdated → LOCAL_HOME_LOCATION_CHANGED sent to a
    confirmed peer at proto_version=5."""
    svc, fed_repo, bus = _make_service()
    await fed_repo.save_instance(_peer("peer-v5", proto_version=5))

    with patch(_SEND_EVENT_PATH, new_callable=AsyncMock) as mock_send:
        await bus.publish(LocalHomeLocationUpdated(latitude=52.52, longitude=13.40))

        mock_send.assert_awaited_once()
        call_kwargs = mock_send.await_args.kwargs
        assert call_kwargs["to_instance_id"] == "peer-v5"
        assert (
            call_kwargs["event_type"] == FederationEventType.LOCAL_HOME_LOCATION_CHANGED
        )
        assert call_kwargs["payload"] == {"latitude": 52.52, "longitude": 13.40}


@pytest.mark.asyncio
async def test_local_home_location_fans_out_to_all_v5_plus_peers():
    """All confirmed peers at proto_version >= 5 receive the envelope."""
    svc, fed_repo, bus = _make_service()
    await fed_repo.save_instance(_peer("peer-v5", proto_version=5))
    await fed_repo.save_instance(_peer("peer-v6", proto_version=6))

    with patch(_SEND_EVENT_PATH, new_callable=AsyncMock) as mock_send:
        await bus.publish(LocalHomeLocationUpdated(latitude=1.0, longitude=2.0))

        assert mock_send.await_count == 2
        sent_ids = {c.kwargs["to_instance_id"] for c in mock_send.await_args_list}
        assert sent_ids == {"peer-v5", "peer-v6"}


@pytest.mark.asyncio
async def test_local_home_location_skips_sub_v5_peer():
    """A confirmed peer at proto_version=4 is silently skipped."""
    svc, fed_repo, bus = _make_service()
    await fed_repo.save_instance(_peer("peer-v4", proto_version=4))

    with patch(_SEND_EVENT_PATH, new_callable=AsyncMock) as mock_send:
        await bus.publish(LocalHomeLocationUpdated(latitude=52.52, longitude=13.40))

        mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_home_location_skips_v4_sends_to_v5():
    """When both a v4 and a v5 peer are present, only v5 receives."""
    svc, fed_repo, bus = _make_service()
    await fed_repo.save_instance(_peer("peer-v4", proto_version=4))
    await fed_repo.save_instance(_peer("peer-v5", proto_version=5))

    with patch(_SEND_EVENT_PATH, new_callable=AsyncMock) as mock_send:
        await bus.publish(LocalHomeLocationUpdated(latitude=10.0, longitude=20.0))

        assert mock_send.await_count == 1
        call_kwargs = mock_send.await_args.kwargs
        assert call_kwargs["to_instance_id"] == "peer-v5"
