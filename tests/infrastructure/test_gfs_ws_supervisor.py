"""Tests for ``GfsWebSocketSupervisor`` (spec §24.12, SH-side reconciler)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from socialhome.domain.federation import GfsConnection
from socialhome.infrastructure.gfs_ws_supervisor import GfsWebSocketSupervisor


# ── Fakes ──────────────────────────────────────────────────────────────────────


class _FakeRepo:
    """Mock of :class:`AbstractGfsConnectionRepo` exposing only ``list_active``."""

    def __init__(self, conns: list[GfsConnection] | None = None) -> None:
        self._conns = list(conns or [])

    async def list_active(self) -> list[GfsConnection]:
        return list(self._conns)

    def set_active(self, conns: list[GfsConnection]) -> None:
        self._conns = list(conns)

    # The supervisor never calls these, but the Protocol shape needs them.
    async def save(self, conn): ...
    async def get(self, gfs_id): ...
    async def update_status(self, gfs_id, status): ...
    async def delete(self, gfs_id): ...
    async def publish_space(self, space_id, gfs_id): ...
    async def unpublish_space(self, space_id, gfs_id): ...
    async def list_publications(self, gfs_id): ...
    async def list_gfs_for_space(self, space_id): ...
    async def count_published_spaces(self, gfs_id): ...
    async def list_publications_all(self): ...


def _make_conn(gfs_id: str, url: str) -> GfsConnection:
    return GfsConnection(
        id=gfs_id,
        gfs_instance_id=f"gfs-inst-{gfs_id}",
        display_name=gfs_id,
        public_key="aa" * 32,
        inbox_url=url,
        status="active",
        paired_at=datetime.now(timezone.utc).isoformat(),
    )


@pytest.fixture
async def http_session():
    async with aiohttp.ClientSession() as session:
        yield session


# ── Tests ──────────────────────────────────────────────────────────────────────


async def test_supervisor_starts_clients_for_active_pairings(http_session):
    repo = _FakeRepo(
        [_make_conn("g1", "http://gfs1.test"), _make_conn("g2", "http://gfs2.test")]
    )

    started: list[str] = []

    class _StubClient:
        def __init__(
            self, *, gfs_url, instance_id, signing_key, session_factory, on_relay
        ):
            self.gfs_url = gfs_url
            started.append(gfs_url)

        async def start(self):
            return None

        async def stop(self):
            return None

    with patch(
        "socialhome.infrastructure.gfs_ws_supervisor.GfsWebSocketClient",
        _StubClient,
    ):
        supervisor = GfsWebSocketSupervisor(
            repo=repo,
            instance_id="sh-1",
            signing_key=b"\x00" * 32,
            session_factory=lambda: http_session,
            on_relay=AsyncMock(),
            reconcile_interval_seconds=0.05,
        )
        await supervisor.start()
        try:
            assert supervisor.client_count() == 2
            assert sorted(started) == ["http://gfs1.test", "http://gfs2.test"]
            assert supervisor.is_running("g1")
            assert supervisor.is_running("g2")
        finally:
            await supervisor.stop()


async def test_supervisor_picks_up_new_pairing_on_reconcile(http_session):
    repo = _FakeRepo([_make_conn("g1", "http://gfs1.test")])
    started: list[str] = []
    stop_calls: list[str] = []

    class _StubClient:
        def __init__(self, *, gfs_url, **_kwargs):
            self.gfs_url = gfs_url
            started.append(gfs_url)

        async def start(self):
            return None

        async def stop(self):
            stop_calls.append(self.gfs_url)

    with patch(
        "socialhome.infrastructure.gfs_ws_supervisor.GfsWebSocketClient",
        _StubClient,
    ):
        supervisor = GfsWebSocketSupervisor(
            repo=repo,
            instance_id="sh-1",
            signing_key=b"\x00" * 32,
            session_factory=lambda: http_session,
            on_relay=AsyncMock(),
            reconcile_interval_seconds=0.05,
        )
        await supervisor.start()
        try:
            assert supervisor.client_count() == 1

            repo.set_active(
                [
                    _make_conn("g1", "http://gfs1.test"),
                    _make_conn("g2", "http://gfs2.test"),
                ]
            )
            for _ in range(50):
                if supervisor.client_count() == 2:
                    break
                await asyncio.sleep(0.02)
            assert supervisor.client_count() == 2
            assert "http://gfs2.test" in started
        finally:
            await supervisor.stop()


async def test_supervisor_stops_clients_for_removed_pairings(http_session):
    repo = _FakeRepo(
        [_make_conn("g1", "http://gfs1.test"), _make_conn("g2", "http://gfs2.test")]
    )
    stops: list[str] = []

    class _StubClient:
        def __init__(self, *, gfs_url, **_kwargs):
            self.gfs_url = gfs_url

        async def start(self):
            return None

        async def stop(self):
            stops.append(self.gfs_url)

    with patch(
        "socialhome.infrastructure.gfs_ws_supervisor.GfsWebSocketClient",
        _StubClient,
    ):
        supervisor = GfsWebSocketSupervisor(
            repo=repo,
            instance_id="sh-1",
            signing_key=b"\x00" * 32,
            session_factory=lambda: http_session,
            on_relay=AsyncMock(),
            reconcile_interval_seconds=0.05,
        )
        await supervisor.start()
        try:
            assert supervisor.client_count() == 2

            # Disconnect g2 — supervisor should drop its client on the next tick.
            repo.set_active([_make_conn("g1", "http://gfs1.test")])
            for _ in range(50):
                if supervisor.client_count() == 1:
                    break
                await asyncio.sleep(0.02)
            assert supervisor.client_count() == 1
            assert "http://gfs2.test" in stops
        finally:
            await supervisor.stop()


async def test_supervisor_stop_closes_all_clients(http_session):
    repo = _FakeRepo(
        [_make_conn("g1", "http://gfs1.test"), _make_conn("g2", "http://gfs2.test")]
    )
    stops: list[str] = []

    class _StubClient:
        def __init__(self, *, gfs_url, **_kwargs):
            self.gfs_url = gfs_url

        async def start(self):
            return None

        async def stop(self):
            stops.append(self.gfs_url)

    with patch(
        "socialhome.infrastructure.gfs_ws_supervisor.GfsWebSocketClient",
        _StubClient,
    ):
        supervisor = GfsWebSocketSupervisor(
            repo=repo,
            instance_id="sh-1",
            signing_key=b"\x00" * 32,
            session_factory=lambda: http_session,
            on_relay=AsyncMock(),
            reconcile_interval_seconds=10.0,
        )
        await supervisor.start()
        await supervisor.stop()
        assert sorted(stops) == ["http://gfs1.test", "http://gfs2.test"]
        assert supervisor.client_count() == 0
