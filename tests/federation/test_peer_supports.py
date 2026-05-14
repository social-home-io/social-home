"""Tests for ``FederationService.peer_supports(min_version=N)``.

Concrete demonstration of the version-gating discipline laid out in
CLAUDE.md ('Federation protocol versioning'): a v2 sender consults
``peer_supports`` before including a v2-only field, so a v1 peer
never sees the field.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)


def _make_service(peer):
    """Build a tiny FederationService stand-in that exposes the same
    ``peer_supports`` method without touching the dozen other deps
    its full constructor needs."""
    from socialhome.federation.federation_service import FederationService

    repo = SimpleNamespace(get_instance=AsyncMock(return_value=peer))
    svc = SimpleNamespace(
        _federation_repo=repo,
        peer_supports=FederationService.peer_supports,
    )

    async def call(instance_id, *, min_version):
        return await FederationService.peer_supports(
            svc, instance_id, min_version=min_version
        )

    return call


def _peer(instance_id: str, *, proto_version: int) -> RemoteInstance:
    return RemoteInstance(
        id=instance_id,
        display_name=instance_id,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="https://example.test/x",
        local_inbox_id="local",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
        proto_version=proto_version,
    )


@pytest.mark.asyncio
async def test_peer_at_version_supports_that_version_and_below():
    """v2 peer supports v2 and v1; not v3."""
    call = _make_service(_peer("p", proto_version=2))
    assert await call("p", min_version=1) is True
    assert await call("p", min_version=2) is True
    assert await call("p", min_version=3) is False


@pytest.mark.asyncio
async def test_peer_at_v1_does_not_support_v2_field():
    """The classic case the gate exists for: v1 peer ⇒ skip the v2 field."""
    call = _make_service(_peer("p", proto_version=1))
    assert await call("p", min_version=2) is False


@pytest.mark.asyncio
async def test_unknown_peer_does_not_support_anything():
    """A peer not in ``remote_instances`` returns ``False`` so the
    sender omits the optional field rather than guessing."""
    call = _make_service(None)
    assert await call("nope", min_version=1) is False
    assert await call("nope", min_version=2) is False


@pytest.mark.asyncio
async def test_repo_failure_is_fail_soft():
    """Repo lookup raising → ``False``. We never crash the outbound
    fan-out because the version check could not be resolved — sending
    the legacy shape is always safer."""
    from socialhome.federation.federation_service import FederationService

    repo = SimpleNamespace(
        get_instance=AsyncMock(side_effect=RuntimeError("db down")),
    )
    svc = SimpleNamespace(_federation_repo=repo)
    result = await FederationService.peer_supports(svc, "p", min_version=2)
    assert result is False
