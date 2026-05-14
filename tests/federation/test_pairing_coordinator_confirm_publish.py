"""``PairingCoordinator.confirm()`` (initiator side of the §11 QR
handshake) must publish :class:`PairingConfirmed` on the local bus so
subscribers — capabilities announcement, peer-directory mirror, DM
history scheduler, space sync scheduler — see the new confirmed peer
without waiting for the next restart.

This historically only fired on the responder side (via
``peer_confirm``), causing the asymmetric "a sees b at proto_v2 but
b sees a at proto_v1" failure the federation-demo's capability
assertion surfaced.
"""

from __future__ import annotations

import asyncio

from socialhome.crypto import (
    generate_identity_keypair,
    generate_x25519_keypair,
)
from socialhome.domain.events import PairingConfirmed
from socialhome.federation.pairing_coordinator import PairingCoordinator
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager


class _FakeRepo:
    def __init__(self) -> None:
        self.instances: dict = {}
        self.pairings: dict = {}

    async def save_instance(self, inst):
        self.instances[inst.id] = inst
        return inst

    async def get_instance(self, iid):
        return self.instances.get(iid)

    async def create_pairing(self, session):
        self.pairings[session.token] = session

    async def get_pairing(self, token):
        return self.pairings.get(token)

    async def delete_pairing(self, token):
        self.pairings.pop(token, None)


def _kek() -> KeyManager:
    import tempfile
    from pathlib import Path

    return KeyManager.from_data_dir(Path(tempfile.mkdtemp()))


async def test_confirm_publishes_pairing_confirmed_on_local_bus():
    """``confirm()`` runs through the accept + confirm flow and the
    resulting bus event lets the local subscriber (capabilities
    announcement, peer directory, …) wake up immediately."""
    captured: list[PairingConfirmed] = []
    bus = EventBus()
    bus.subscribe(PairingConfirmed, captured.append)

    kp = generate_identity_keypair()
    repo = _FakeRepo()
    coord = PairingCoordinator(repo, _kek(), kp.public_key, bus=bus)

    peer_kp = generate_identity_keypair()
    peer_dh = generate_x25519_keypair()
    accept_result = await coord.accept(
        {
            "token": "tok-confirm-publish",
            "identity_pk": peer_kp.public_key.hex(),
            "dh_pk": peer_dh.public_key.hex(),
            "inbox_url": "https://peer/wh",
        }
    )
    confirmed = await coord.confirm(
        "tok-confirm-publish",
        accept_result["verification_code"],
    )

    # The bus is async, so we have to flush queued events before
    # asserting. Both sides of the publish are inside ``confirm()``;
    # one ``await`` cycle is enough to drain the in-process delivery.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(captured) == 1, (
        f"expected one PairingConfirmed publish, got {len(captured)}"
    )
    assert captured[0].instance_id == confirmed.id


async def test_confirm_skips_publish_when_no_bus_wired():
    """In-test or standalone constructions that don't pass a bus
    must not raise — confirm() should fall through gracefully."""
    kp = generate_identity_keypair()
    repo = _FakeRepo()
    # No bus= kwarg.
    coord = PairingCoordinator(repo, _kek(), kp.public_key)
    peer_kp = generate_identity_keypair()
    peer_dh = generate_x25519_keypair()
    accept_result = await coord.accept(
        {
            "token": "tok-no-bus",
            "identity_pk": peer_kp.public_key.hex(),
            "dh_pk": peer_dh.public_key.hex(),
            "inbox_url": "https://peer/wh",
        }
    )
    confirmed = await coord.confirm(
        "tok-no-bus",
        accept_result["verification_code"],
    )
    assert confirmed.status.value == "confirmed"
