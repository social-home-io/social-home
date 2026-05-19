"""Tests for the shared HA-home-location persistence helper."""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import LocalHomeLocationUpdated
from socialhome.infrastructure.event_bus import EventBus
from socialhome.platform.ha_home_location import (
    persist_home_location_from_ha,
)


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.iid = iid
    yield e
    await db.shutdown()


async def test_persist_writes_initial_value_and_publishes(env):
    """First boot — coords were NULL, HA reports a real value →
    written + LocalHomeLocationUpdated fired."""
    bus = EventBus()
    received: list[LocalHomeLocationUpdated] = []

    async def _record(e):
        received.append(e)

    bus.subscribe(LocalHomeLocationUpdated, _record)

    await persist_home_location_from_ha(
        db=env.db,
        bus=bus,
        latitude=52.52,
        longitude=13.40,
    )

    row = await env.db.fetchone(
        "SELECT home_lat, home_lon FROM instance_identity WHERE id='self'",
    )
    assert row["home_lat"] == 52.52
    assert row["home_lon"] == 13.40
    assert len(received) == 1
    assert received[0].latitude == 52.52
    assert received[0].longitude == 13.40


async def test_persist_truncates_to_4dp(env):
    """Inputs above 4dp precision are rounded — §25 invariant."""
    bus = EventBus()
    await persist_home_location_from_ha(
        db=env.db,
        bus=bus,
        latitude=52.523456,
        longitude=13.401234,
    )
    row = await env.db.fetchone(
        "SELECT home_lat, home_lon FROM instance_identity WHERE id='self'",
    )
    assert row["home_lat"] == 52.5235
    assert row["home_lon"] == 13.4012


async def test_persist_noop_when_unchanged(env):
    """Same value as already stored: no UPDATE, no event."""
    bus = EventBus()
    received: list[LocalHomeLocationUpdated] = []

    async def _record(e):
        received.append(e)

    bus.subscribe(LocalHomeLocationUpdated, _record)

    await persist_home_location_from_ha(
        db=env.db,
        bus=bus,
        latitude=52.52,
        longitude=13.40,
    )
    received.clear()

    # Second call with same value.
    await persist_home_location_from_ha(
        db=env.db,
        bus=bus,
        latitude=52.52,
        longitude=13.40,
    )
    assert received == []


async def test_persist_skips_zero_zero(env):
    """HA returns 0.0/0.0 when operator hasn't set a location.
    Don't write garbage; don't publish."""
    bus = EventBus()
    received: list[LocalHomeLocationUpdated] = []

    async def _record(e):
        received.append(e)

    bus.subscribe(LocalHomeLocationUpdated, _record)

    await persist_home_location_from_ha(
        db=env.db,
        bus=bus,
        latitude=0.0,
        longitude=0.0,
    )
    row = await env.db.fetchone(
        "SELECT home_lat, home_lon FROM instance_identity WHERE id='self'",
    )
    assert row["home_lat"] is None
    assert row["home_lon"] is None
    assert received == []
