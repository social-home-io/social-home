"""Tests for the in-memory GFS relay bridge (public-content proxy fallback)."""

from __future__ import annotations

import asyncio

import pytest

from socialhome.global_server.relay_bridge import RelayBridge


async def _drain(bridge: RelayBridge, relay_id: str) -> list[bytes]:
    return [chunk async for chunk in bridge.consume(relay_id)]


async def test_feed_then_finish_round_trips_in_order():
    bridge = RelayBridge()
    relay_id = bridge.create(target_instance_id="inst-a", scope="s-1")
    consumer = asyncio.create_task(_drain(bridge, relay_id))

    assert await bridge.feed(relay_id, b"one")
    assert await bridge.feed(relay_id, b"two")
    await bridge.finish(relay_id)

    assert await consumer == [b"one", b"two"]
    # Channel dropped after the stream ends.
    assert bridge.get(relay_id) is None


async def test_connected_event_flips_on_first_feed():
    bridge = RelayBridge()
    relay_id = bridge.create(target_instance_id="inst-a")
    channel = bridge.get(relay_id)
    assert channel is not None
    assert not channel.connected.is_set()
    await bridge.feed(relay_id, b"x")
    assert channel.connected.is_set()


async def test_finish_alone_marks_connected_and_ends_empty():
    bridge = RelayBridge()
    relay_id = bridge.create(target_instance_id="inst-a")
    channel = bridge.get(relay_id)
    consumer = asyncio.create_task(_drain(bridge, relay_id))
    await bridge.finish(relay_id)
    assert await consumer == []
    assert channel is not None and channel.connected.is_set()


async def test_feed_unknown_relay_returns_false():
    bridge = RelayBridge()
    assert await bridge.feed("missing", b"x") is False


async def test_finish_unknown_relay_is_noop():
    bridge = RelayBridge()
    await bridge.finish("missing")  # must not raise


async def test_consume_unknown_relay_yields_nothing():
    bridge = RelayBridge()
    assert await _drain(bridge, "missing") == []


async def test_close_drops_channel():
    bridge = RelayBridge()
    relay_id = bridge.create(target_instance_id="inst-a")
    bridge.close(relay_id)
    assert bridge.get(relay_id) is None


async def test_create_returns_distinct_ids():
    bridge = RelayBridge()
    a = bridge.create(target_instance_id="inst-a")
    b = bridge.create(target_instance_id="inst-a")
    assert a != b


async def test_gc_expired_evicts_only_old_channels():
    bridge = RelayBridge()
    fresh = bridge.create(target_instance_id="inst-a")
    stale = bridge.create(target_instance_id="inst-b")
    # Backdate the stale channel well past the TTL.
    bridge.get(stale).created_at = 0.0
    evicted = bridge.gc_expired(now=10_000.0)
    assert evicted == 1
    assert bridge.get(stale) is None
    assert bridge.get(fresh) is not None


async def test_target_instance_recorded():
    bridge = RelayBridge()
    relay_id = bridge.create(target_instance_id="inst-xyz", scope="s-9")
    channel = bridge.get(relay_id)
    assert channel is not None
    assert channel.target_instance_id == "inst-xyz"
    assert channel.scope == "s-9"


@pytest.mark.parametrize("n", [1, 5, 50])
async def test_backpressure_does_not_drop_chunks(n):
    """The bounded queue throttles but never loses chunks."""
    bridge = RelayBridge()
    relay_id = bridge.create(target_instance_id="inst-a")
    payloads = [f"chunk-{i}".encode() for i in range(n)]

    async def feeder():
        for p in payloads:
            await bridge.feed(relay_id, p)
        await bridge.finish(relay_id)

    feed_task = asyncio.create_task(feeder())
    got = await _drain(bridge, relay_id)
    await feed_task
    assert got == payloads
