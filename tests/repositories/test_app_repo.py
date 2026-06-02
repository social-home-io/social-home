"""Tests for SqliteAppRepo (installed_apps table).

Fixture choices
---------------
* Uses the shared ``db`` fixture from ``tests/conftest.py`` — a
  fully-migrated ``AsyncDatabase`` with ``PRAGMA foreign_keys=ON``.
* ``installed_by=None`` throughout: FK enforcement is ON in the test DB
  (confirmed in ``AsyncDatabase.startup``), so inserting a bare user-id
  string that has no matching ``users`` row would raise an integrity error.
  ``NULL`` is the honest representation of "installed without a tracked user"
  and is explicitly allowed by the schema (``TEXT REFERENCES … ON DELETE SET
  NULL``).

Enqueue ordering
----------------
``AsyncDatabase.enqueue`` awaits the future that the writer resolves once the
batch is committed, so a subsequent ``fetchone`` / ``fetchall`` always sees the
committed row — no extra flush or sleep is needed.
"""

from __future__ import annotations

import pytest

from socialhome.domain.apps import AppManifest, InstalledApp
from socialhome.repositories.app_repo import SqliteAppRepo


def _app(
    app_id: str = "chess", version: str = "1.0.0", enabled: bool = True
) -> InstalledApp:
    return InstalledApp(
        app_id=app_id,
        name="Chess",
        version=version,
        enabled=enabled,
        manifest=AppManifest(entry="index.html", icon=None, capabilities=("storage",)),
        bundle_path=f"apps/{app_id}/{version}",
        bundle_sha256="ab" * 32,
        source_url="https://example/chess.tgz",
        installed_by=None,  # FK enforcement is ON; no real user row available
        installed_at="2026-06-02T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_install_and_get_roundtrip(db):
    repo = SqliteAppRepo(db)
    await repo.install(_app())
    got = await repo.get("chess")
    assert got is not None
    assert got.name == "Chess"
    assert got.manifest.entry == "index.html"
    assert got.enabled is True


@pytest.mark.asyncio
async def test_get_missing_returns_none(db):
    repo = SqliteAppRepo(db)
    assert await repo.get("nope") is None


@pytest.mark.asyncio
async def test_list_installed(db):
    repo = SqliteAppRepo(db)
    await repo.install(_app(app_id="chess"))
    await repo.install(_app(app_id="notes"))
    rows = await repo.list_installed()
    assert {r.app_id for r in rows} == {"chess", "notes"}


@pytest.mark.asyncio
async def test_set_enabled_and_uninstall(db):
    repo = SqliteAppRepo(db)
    await repo.install(_app())
    await repo.set_enabled("chess", enabled=False)
    assert (await repo.get("chess")).enabled is False
    await repo.uninstall("chess")
    assert await repo.get("chess") is None
