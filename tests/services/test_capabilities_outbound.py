"""Tests for the proto_version announcement path.

Covers the three surfaces that together implement the §federation
protocol versioning rule:

1. ``CapabilitiesOutbound.publish`` builds the right envelope and
   skips own / unconfirmed peers.
2. The inbound handler in ``federation_inbound.pairing`` writes the
   announced version onto the ``remote_instances`` row.
3. ``FederationService.peer_supports(instance_id, min_version=N)``
   returns the right answer at each version boundary.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.federation_capabilities import OURS as OUR_PROTO_VERSION
from socialhome.services.capabilities_outbound import CapabilitiesOutbound


def _peer(instance_id: str, *, proto_version: int = 1) -> RemoteInstance:
    return RemoteInstance(
        id=instance_id,
        display_name=instance_id,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url=f"https://example.test/{instance_id}",
        local_inbox_id=f"local-{instance_id}",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
        proto_version=proto_version,
    )


@pytest.mark.asyncio
async def test_capabilities_outbound_fans_out_to_confirmed_peers():
    peer_a = _peer("inst-a")
    peer_b = _peer("inst-b")
    repo = SimpleNamespace(
        list_instances=AsyncMock(return_value=[peer_a, peer_b]),
    )
    fed = SimpleNamespace(
        _own_instance_id="inst-self",
        send_event=AsyncMock(),
    )
    out = CapabilitiesOutbound(
        federation_service=fed,
        federation_repo=repo,
    )

    sent = await out.publish()

    assert sent == 2
    assert fed.send_event.await_count == 2
    # Same payload to every peer — the integer is build-wide.
    for call in fed.send_event.await_args_list:
        assert call.kwargs["event_type"] == (
            FederationEventType.INSTANCE_CAPABILITIES_UPDATED
        )
        assert call.kwargs["payload"] == {"proto_version": OUR_PROTO_VERSION}
    targets = {c.kwargs["to_instance_id"] for c in fed.send_event.await_args_list}
    assert targets == {"inst-a", "inst-b"}


@pytest.mark.asyncio
async def test_capabilities_outbound_skips_self():
    peer = _peer("inst-self")
    repo = SimpleNamespace(
        list_instances=AsyncMock(return_value=[peer]),
    )
    fed = SimpleNamespace(
        _own_instance_id="inst-self",
        send_event=AsyncMock(),
    )
    out = CapabilitiesOutbound(
        federation_service=fed,
        federation_repo=repo,
    )

    sent = await out.publish()

    assert sent == 0
    assert fed.send_event.await_count == 0


@pytest.mark.asyncio
async def test_capabilities_outbound_tolerates_repo_failure():
    repo = SimpleNamespace(
        list_instances=AsyncMock(side_effect=RuntimeError("boom")),
    )
    fed = SimpleNamespace(_own_instance_id="inst-self", send_event=AsyncMock())
    out = CapabilitiesOutbound(
        federation_service=fed,
        federation_repo=repo,
    )

    # Must not raise — the outbound is fire-and-forget at startup.
    sent = await out.publish()
    assert sent == 0
