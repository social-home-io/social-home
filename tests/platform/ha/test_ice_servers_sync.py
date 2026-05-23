"""Tests for :class:`HaIceServerSync` — HA Core → SH ICE-server pull."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from socialhome.platform.ha.ice_servers_sync import HaIceServerSync


# ─── _fetch shape handling ────────────────────────────────────────────────


async def test_fetch_and_apply_once_normalises_chrome_shape():
    """HA's ``web_rtc/ice_servers`` reply comes through as a list of
    Chrome-shaped dicts (``urls`` may be a string OR a list). The
    sync helper normalises ``urls`` to a list every time so the
    downstream federation transport's ``_build_rtc_config`` sees
    one shape — without this every TURN entry from a vanilla
    Nabu Casa Cloud setup (which emits ``"urls": "turn:…"``)
    would fall through with the wrong type."""
    client = AsyncMock()
    client.ws_command.return_value = {
        "result": [
            {"urls": "stun:stun.home-assistant.io:80"},
            {
                "urls": ["turn:t.example:3478", "turns:t.example:5349"],
                "username": "u",
                "credential": "c",
            },
        ]
    }
    applied: list[list[dict]] = []

    async def _apply(servers):
        applied.append(servers)

    sync = HaIceServerSync(client=client, apply_callback=_apply)
    ok = await sync.fetch_and_apply_once()

    assert ok is True
    assert applied == [
        [
            {"urls": ["stun:stun.home-assistant.io:80"]},
            {
                "urls": ["turn:t.example:3478", "turns:t.example:5349"],
                "username": "u",
                "credential": "c",
            },
        ]
    ]


async def test_fetch_skips_malformed_entries_keeps_good_ones():
    """A future HA core version that emits a new field shape (or a
    misbehaving runtime ICE provider that pushes garbage) shouldn't
    nuke the whole list — drop just the bad entries and apply the
    rest."""
    client = AsyncMock()
    client.ws_command.return_value = {
        "result": [
            "not a dict",
            {"no_urls_key": "x"},
            {"urls": ""},  # empty string → empty list → drop
            {"urls": ["stun:good.example"]},
        ]
    }
    applied: list[list[dict]] = []

    async def _apply(servers):
        applied.append(servers)

    sync = HaIceServerSync(client=client, apply_callback=_apply)
    await sync.fetch_and_apply_once()
    assert applied == [[{"urls": ["stun:good.example"]}]]


async def test_fetch_returns_false_on_ws_failure(caplog):
    """When the WS handshake fails (HaClient.ws_command returns
    ``None``), the apply callback must NOT fire and the result is
    a clean ``False`` — the scheduler then uses its
    ``error_retry_s`` backoff instead of the regular
    ``interval_s``."""
    client = AsyncMock()
    client.ws_command.return_value = None
    apply = AsyncMock()

    sync = HaIceServerSync(client=client, apply_callback=apply)
    ok = await sync.fetch_and_apply_once()

    assert ok is False
    apply.assert_not_awaited()


async def test_fetch_returns_false_when_result_not_a_list(caplog):
    """Defensive: if HA core ever shipped a result shape other than
    a list (e.g. wrapped in a dict in a future version), drop it
    and try again rather than crashing."""
    import logging

    caplog.set_level(logging.WARNING, logger="socialhome.platform.ha.ice_servers_sync")
    client = AsyncMock()
    client.ws_command.return_value = {"result": {"unexpected": "shape"}}
    apply = AsyncMock()

    sync = HaIceServerSync(client=client, apply_callback=apply)
    ok = await sync.fetch_and_apply_once()

    assert ok is False
    apply.assert_not_awaited()
    assert any("not a list" in rec.message for rec in caplog.records)


# ─── Scheduler lifecycle ──────────────────────────────────────────────────


async def test_start_runs_initial_fetch_then_waits_interval():
    """``start()`` kicks the background loop; the first iteration
    fetches immediately so the federation transport sees fresh
    ICE state without waiting 24 h."""
    client = AsyncMock()
    client.ws_command.return_value = {"result": [{"urls": ["stun:ok.example"]}]}
    applied: list = []

    async def _apply(servers):
        applied.append(servers)

    sync = HaIceServerSync(
        client=client,
        apply_callback=_apply,
        # Wide intervals — we only care that the FIRST tick fired,
        # not what cadence subsequent ones run at.
        interval_s=1000.0,
        error_retry_s=1000.0,
    )
    await sync.start()
    # Yield to the loop until the first fetch lands. ``ws_command``
    # is awaited inside the loop body; loop is cooperative.
    for _ in range(20):
        await asyncio.sleep(0.005)
        if applied:
            break
    assert applied  # first fetch landed
    await sync.stop()


async def test_stop_is_idempotent():
    """A second ``stop()`` while the task is already gone is a
    no-op — matters for app cleanup paths that may double-fire."""
    client = AsyncMock()
    client.ws_command.return_value = {"result": []}

    async def _apply(_servers):
        pass

    sync = HaIceServerSync(client=client, apply_callback=_apply)
    await sync.start()
    await sync.stop()
    await sync.stop()  # second stop — must not raise


async def test_start_is_idempotent():
    """A second ``start()`` while the loop is already running is a
    no-op — matters when an adapter's ``on_startup`` is re-entered
    by a test harness or a future hot-reload path."""
    client = AsyncMock()
    client.ws_command.return_value = {"result": []}

    async def _apply(_servers):
        pass

    sync = HaIceServerSync(client=client, apply_callback=_apply)
    await sync.start()
    task1 = sync._task
    await sync.start()
    assert sync._task is task1  # same task; not spawned again
    await sync.stop()


# ─── Loop backoff ─────────────────────────────────────────────────────────


async def test_loop_uses_error_retry_after_failure():
    """A failed fetch must wait ``error_retry_s`` before retrying
    (not the full daily interval). Without this an HA Core that's
    booting after SH would be invisible to the federation transport
    for 24 h."""
    client = AsyncMock()
    # First call fails (None); second succeeds. The scheduler should
    # arrive at the success on its own.
    client.ws_command.side_effect = [None, {"result": [{"urls": ["stun:r.example"]}]}]
    applied: list = []

    async def _apply(servers):
        applied.append(servers)

    sync = HaIceServerSync(
        client=client,
        apply_callback=_apply,
        interval_s=1000.0,
        error_retry_s=0.01,  # near-immediate retry
    )
    await sync.start()
    for _ in range(50):
        await asyncio.sleep(0.005)
        if applied:
            break
    assert applied == [[{"urls": ["stun:r.example"]}]]
    assert client.ws_command.await_count >= 2
    await sync.stop()
