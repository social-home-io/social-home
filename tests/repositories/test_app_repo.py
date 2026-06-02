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

import uuid

import pytest

from socialhome.domain.apps import AppManifest, InstalledApp
from socialhome.domain.user import User
from socialhome.repositories.app_repo import SqliteAppRepo
from socialhome.repositories.user_repo import SqliteUserRepo


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


async def _seed_user(db) -> str:
    """Insert a minimal local user row and return its user_id."""
    user_id = uuid.uuid4().hex
    username = f"testuser_{user_id[:8]}"
    user = User(user_id=user_id, username=username, display_name="Test User")
    await SqliteUserRepo(db).save(user)
    return user_id


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


# ── KV store tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kv_set_get_roundtrip(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.kv_set(
        "chess", uid, "game:1", '{"turn":"w"}', "2026-06-02T00:00:00+00:00"
    )
    got = await repo.kv_get("chess", uid, "game:1")
    assert got is not None and got.value_json == '{"turn":"w"}'


@pytest.mark.asyncio
async def test_kv_set_upserts(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.kv_set("chess", uid, "k", '"a"', "2026-06-02T00:00:00+00:00")
    await repo.kv_set("chess", uid, "k", '"b"', "2026-06-02T00:01:00+00:00")
    assert (await repo.kv_get("chess", uid, "k")).value_json == '"b"'
    assert await repo.kv_count("chess", uid) == 1


@pytest.mark.asyncio
async def test_kv_list_and_delete(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.kv_set("chess", uid, "a", "1", "2026-06-02T00:00:00+00:00")
    await repo.kv_set("chess", uid, "b", "2", "2026-06-02T00:00:00+00:00")
    assert [e.key for e in await repo.kv_list("chess", uid)] == ["a", "b"]
    await repo.kv_delete("chess", uid, "a")
    assert {e.key for e in await repo.kv_list("chess", uid)} == {"b"}


@pytest.mark.asyncio
async def test_kv_cascades_on_uninstall(db):
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.kv_set("chess", uid, "k", "1", "2026-06-02T00:00:00+00:00")
    await repo.uninstall("chess")
    assert await repo.kv_get("chess", uid, "k") is None
    assert await repo.kv_count("chess", uid) == 0


@pytest.mark.asyncio
async def test_kv_cascades_on_user_delete(db):
    """Deleting a user row must cascade-delete all their app_kv entries.

    Proves the ``FOREIGN KEY (user_id) REFERENCES users(user_id)
    ON DELETE CASCADE`` constraint on the ``app_kv`` table.
    """
    repo = SqliteAppRepo(db)
    uid = await _seed_user(db)
    await repo.install(_app())
    await repo.kv_set(
        "chess", uid, "game:1", '{"turn":"w"}', "2026-06-02T00:00:00+00:00"
    )
    # Confirm the row is there before deletion.
    assert await repo.kv_count("chess", uid) == 1

    # Hard-delete the user row; no user_repo.delete method exists so we use
    # direct SQL (acceptable: this is a repo-layer test probing the DB schema).
    await db.enqueue("DELETE FROM users WHERE user_id = ?", (uid,))

    # The cascade must have removed the kv row.
    assert await repo.kv_count("chess", uid) == 0
    assert await repo.kv_get("chess", uid, "game:1") is None
