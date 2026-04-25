"""Shared fixtures for GFS tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from socialhome.db.database import AsyncDatabase

_GFS_MIGRATIONS = Path(__file__).resolve().parent.parent.parent / (
    "socialhome/global_server/migrations"
)


# Some dev environments transitively pull in
# ``pytest-homeassistant-custom-component``, which globally disables
# sockets via ``pytest-socket``. GFS tests use real ``aiohttp.TestServer``
# loopback connections, so re-enable sockets when both plugins are
# present. CI does not install either plugin; the fixture simply does
# not register and tests run normally.
try:
    import pytest_socket  # noqa: F401

    @pytest.fixture(autouse=True)
    def _enable_sockets(socket_enabled):
        """Re-enable sockets if the HA pytest plugin disabled them."""

except ImportError:  # pragma: no cover - CI path
    pass


@pytest.fixture
async def gfs_db(tmp_dir):
    """AsyncDatabase pointed at a temp GFS database with migrations applied."""
    db = AsyncDatabase(
        tmp_dir / "gfs.db",
        migrations_dir=_GFS_MIGRATIONS,
        batch_timeout_ms=10,
    )
    await db.startup()
    yield db
    await db.shutdown()
