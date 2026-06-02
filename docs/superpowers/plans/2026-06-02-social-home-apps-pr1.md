# Social Home Apps — Implementation Plan (PR1: App Registry & Install)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **On execution start:** copy this file to the canonical superpowers location `docs/superpowers/plans/2026-06-02-social-home-apps-pr1.md` (plan-mode wrote it to the plan dir because that was the only writable path).

**Goal:** Let a household admin browse a remote catalog of embedded JS apps, install one (download → sha256-verify → unpack locally), and see installed apps listed — the foundation for the chess demo.

**Architecture:** Mirror the canonical `preferences` full-stack pattern (domain frozen dataclass → `Abstract*Repo` Protocol + `Sqlite*Repo` → service with admin guard + bus events → `BaseView` routes → migration → `app.py` wiring). Apps are fetched from a separate `socialhome-apps` repo's GitHub release (`catalog.json` + per-app tarball), verified by sha256, unpacked under `media_path/apps/<app_id>/<version>/`, and tracked in a new `installed_apps` table.

**Tech Stack:** Python 3.14 / aiohttp / aiosqlite (`AsyncDatabase.enqueue`/`fetchone`/`fetchall`), `aiohttp.ClientSession` for catalog/bundle fetch, `aiofiles` + `aiofiles.os` for async FS, `asyncio.to_thread` for `tarfile`/`hashlib` (CPU-bound), Preact/TS SPA.

---

## Scope note (superpowers writing-plans Scope Check)

The full feature is **five independent subsystems**, each shippable on its own. This plan covers **PR1 only**. PR2–PR5 each get their own `writing-plans` pass when started (roadmap at the bottom):

- **PR1 (this plan)** — registry + install/uninstall/list + Browse UI.
- **PR2** — per-user KV storage API (`app_kv`, quota, store routes, SDK `sh.store.*`).
- **PR3** — sandboxed-iframe runtime + `postMessage` bridge + signed bundle serving (the §2 third-party-trust sign-off PR).
- **PR4** — `fed-app-v1` federation DataChannel + `APP_SESSION` + capability bump.
- **PR5** — `socialhome-apps` repo: app SDK + chess demo + release workflow.

**Locked decisions:** release-fetch catalog · dedicated `fed-app-v1` channel (modeled on `fed-media-v1`) · per-user-per-app KV · sandboxed-iframe runtime.

---

## File Structure (PR1)

- Create `socialhome/migrations/0020_installed_apps.sql` — `installed_apps` table.
- Create `socialhome/domain/apps.py` — `InstalledApp`, `AppManifest`, `AppCatalogEntry` frozen dataclasses + exceptions.
- Create `socialhome/repositories/app_repo.py` — `AbstractAppRepo` Protocol + `SqliteAppRepo`.
- Create `socialhome/services/app_catalog_service.py` — fetch + parse remote `catalog.json`.
- Create `socialhome/services/app_service.py` — install/uninstall/list/enable; admin guard; download+verify+unpack.
- Create `socialhome/routes/apps.py` — `AppCollectionView`, `AppDetailView`, `AppCatalogView`.
- Modify `socialhome/app_keys.py` — add `app_service_key`.
- Modify `socialhome/app.py` — `_build_repos` (`app=SqliteAppRepo(db)`), `_build_services` (construct + register `app_service`).
- Modify `socialhome/routes/__init__.py` — import + `add_view` three routes.
- Create tests: `tests/repositories/test_app_repo.py`, `tests/services/test_app_catalog_service.py`, `tests/services/test_app_service.py`, `tests/routes/test_apps.py`.
- Create SPA: `client/src/features/apps/AppsPage.tsx`, `client/src/store/apps.ts`; modify `client/src/router.ts`, `client/src/components/SideNav.tsx`, `client/src/features/dashboard/DashboardPage.tsx`.
- Modify docs: `docs/api.md`, `docs/database.md`, `docs/architecture.md`, `docs/principles.md`.

---

## Task 1: Migration — `installed_apps` table

**Files:**
- Create: `socialhome/migrations/0020_installed_apps.sql`
- Test: `tests/test_migrations.py` (existing migration-applies test picks it up; verify below)

- [ ] **Step 1: Write the migration** (next number after `0019_drop_link_join_mode.sql`)

```sql
-- Social Home Apps: registry of admin-installed embedded JS apps.
--
-- Migration audit (CLAUDE.md):
--   1. Audited existing tables — no registry/extension table exists; the
--      preferences table is scoped to fixed feature toggles, not an open
--      set of installable apps, so it can't host this.
--   2. Alternative rejected — a JSON blob column on `preferences` would
--      conflate admin toggles with an unbounded app list and lose per-app
--      FK-cascade cleanup (needed for PR2's app_kv). A first-class table is
--      the smallest shape that supports cascade-on-uninstall.
--   3. Smallest change — additive CREATE TABLE only; no existing row touched.

CREATE TABLE installed_apps (
    app_id        TEXT PRIMARY KEY,           -- catalog slug, e.g. 'chess'
    name          TEXT NOT NULL,
    version       TEXT NOT NULL,              -- semver of installed bundle
    enabled       INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    manifest_json TEXT NOT NULL DEFAULT '{}', -- capabilities, entry file, icon
    bundle_path   TEXT NOT NULL,              -- relative dir under media_path/apps/
    bundle_sha256 TEXT NOT NULL,              -- verified at install
    source_url    TEXT NOT NULL,              -- release asset URL it came from
    installed_by  TEXT REFERENCES users(user_id) ON DELETE SET NULL,
    installed_at  TEXT NOT NULL               -- UTC ISO 8601 ('…+00:00')
);
```

- [ ] **Step 2: Run the migration test to verify the schema applies**

Run: `pytest tests/test_migrations.py -v`
Expected: PASS (all migrations apply in order on a fresh DB; the new file is picked up by the numeric scan).

- [ ] **Step 3: Commit**

```bash
git add socialhome/migrations/0020_installed_apps.sql
git commit -m "feat: installed_apps registry table (Social Home Apps)"
```

---

## Task 2: Domain dataclasses + exceptions

**Files:**
- Create: `socialhome/domain/apps.py`
- Test: `tests/domain/test_apps.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/domain/test_apps.py
import dataclasses
import pytest
from socialhome.domain.apps import (
    InstalledApp, AppManifest, AppCatalogEntry,
    AppNotFoundError, AppNotEnabledError,
)


def test_installed_app_is_frozen():
    app = InstalledApp(
        app_id="chess", name="Chess", version="1.0.0", enabled=True,
        manifest=AppManifest(entry="index.html", icon=None, capabilities=("storage",)),
        bundle_path="apps/chess/1.0.0", bundle_sha256="ab" * 32,
        source_url="https://example/chess-1.0.0.tgz",
        installed_by="u1", installed_at="2026-06-02T00:00:00+00:00",
    )
    assert app.app_id == "chess"
    with pytest.raises(dataclasses.FrozenInstanceError):
        app.app_id = "other"  # type: ignore[misc]


def test_catalog_entry_from_dict_validates_required_fields():
    entry = AppCatalogEntry.from_dict({
        "app_id": "chess", "name": "Chess", "latest_version": "1.0.0",
        "description": "Play chess", "icon_url": None,
        "capabilities": ["storage", "federation"],
        "bundle_url": "https://example/chess-1.0.0.tgz",
        "bundle_sha256": "cd" * 32,
    })
    assert entry.app_id == "chess"
    assert entry.bundle_sha256 == "cd" * 32
    with pytest.raises(ValueError):
        AppCatalogEntry.from_dict({"app_id": "x"})  # missing fields


def test_app_not_found_is_keyerror_subclass():
    assert issubclass(AppNotFoundError, Exception)
    assert issubclass(AppNotEnabledError, Exception)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/domain/test_apps.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'socialhome.domain.apps'`

- [ ] **Step 3: Write the implementation**

```python
# socialhome/domain/apps.py
"""Social Home Apps — domain dataclasses for the app registry.

Pure data, no I/O. ``InstalledApp`` is the row shape of the
``installed_apps`` table; ``AppManifest`` is the parsed per-app
``manifest.json``; ``AppCatalogEntry`` is one item of the remote
``catalog.json`` published by the ``socialhome-apps`` repo.
"""

from __future__ import annotations

from dataclasses import dataclass


class AppError(Exception):
    """Base for app-registry domain errors."""


class AppNotFoundError(AppError):
    """No installed app with the given id."""


class AppNotEnabledError(AppError):
    """The app exists but is disabled by the admin."""


class AppAlreadyInstalledError(AppError):
    """Install requested for an app id that is already installed."""


class AppIntegrityError(AppError):
    """Downloaded bundle failed sha256 / manifest / path validation."""


@dataclass(slots=True, frozen=True)
class AppManifest:
    """Parsed ``manifest.json`` from an app bundle."""

    entry: str                              # relative HTML entry, e.g. 'index.html'
    icon: str | None                        # relative icon path or None
    capabilities: tuple[str, ...]           # e.g. ('storage', 'federation')

    @classmethod
    def from_dict(cls, data: dict) -> "AppManifest":
        entry = data.get("entry")
        if not isinstance(entry, str) or not entry:
            raise ValueError("manifest.entry must be a non-empty string")
        if "/" in entry.lstrip("./") and entry.startswith(("/", "..")):
            raise ValueError("manifest.entry must be a relative path inside the bundle")
        caps = data.get("capabilities", [])
        if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
            raise ValueError("manifest.capabilities must be a list of strings")
        icon = data.get("icon")
        if icon is not None and not isinstance(icon, str):
            raise ValueError("manifest.icon must be a string or null")
        return cls(entry=entry, icon=icon, capabilities=tuple(caps))


@dataclass(slots=True, frozen=True)
class InstalledApp:
    """Row shape of the ``installed_apps`` table."""

    app_id: str
    name: str
    version: str
    enabled: bool
    manifest: AppManifest
    bundle_path: str
    bundle_sha256: str
    source_url: str
    installed_by: str | None
    installed_at: str


@dataclass(slots=True, frozen=True)
class AppCatalogEntry:
    """One entry of the remote ``catalog.json``."""

    app_id: str
    name: str
    latest_version: str
    description: str
    icon_url: str | None
    capabilities: tuple[str, ...]
    bundle_url: str
    bundle_sha256: str

    _REQUIRED = (
        "app_id", "name", "latest_version", "description",
        "bundle_url", "bundle_sha256",
    )

    @classmethod
    def from_dict(cls, data: dict) -> "AppCatalogEntry":
        missing = [k for k in cls._REQUIRED if not data.get(k)]
        if missing:
            raise ValueError(f"catalog entry missing fields: {missing}")
        caps = data.get("capabilities", [])
        if not isinstance(caps, list):
            raise ValueError("catalog entry capabilities must be a list")
        return cls(
            app_id=str(data["app_id"]),
            name=str(data["name"]),
            latest_version=str(data["latest_version"]),
            description=str(data["description"]),
            icon_url=data.get("icon_url"),
            capabilities=tuple(str(c) for c in caps),
            bundle_url=str(data["bundle_url"]),
            bundle_sha256=str(data["bundle_sha256"]),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/domain/test_apps.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add socialhome/domain/apps.py tests/domain/test_apps.py
git commit -m "feat: app-registry domain dataclasses"
```

---

## Task 3: Repository — `AbstractAppRepo` + `SqliteAppRepo`

**Files:**
- Create: `socialhome/repositories/app_repo.py`
- Test: `tests/repositories/test_app_repo.py`

- [ ] **Step 1: Write the failing test** (uses the real in-`tmp_path` SQLite + migrations harness; reuse the existing `db`/`migrated_db` fixture — confirm its name in `tests/conftest.py` and substitute if different)

```python
# tests/repositories/test_app_repo.py
import pytest
from socialhome.domain.apps import InstalledApp, AppManifest
from socialhome.repositories.app_repo import SqliteAppRepo


def _app(app_id="chess", version="1.0.0", enabled=True) -> InstalledApp:
    return InstalledApp(
        app_id=app_id, name="Chess", version=version, enabled=enabled,
        manifest=AppManifest(entry="index.html", icon=None, capabilities=("storage",)),
        bundle_path=f"apps/{app_id}/{version}", bundle_sha256="ab" * 32,
        source_url="https://example/chess.tgz",
        installed_by="u1", installed_at="2026-06-02T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_install_and_get_roundtrip(migrated_db):
    repo = SqliteAppRepo(migrated_db)
    await repo.install(_app())
    got = await repo.get("chess")
    assert got is not None
    assert got.name == "Chess"
    assert got.manifest.entry == "index.html"
    assert got.enabled is True


@pytest.mark.asyncio
async def test_get_missing_returns_none(migrated_db):
    repo = SqliteAppRepo(migrated_db)
    assert await repo.get("nope") is None


@pytest.mark.asyncio
async def test_list_installed(migrated_db):
    repo = SqliteAppRepo(migrated_db)
    await repo.install(_app(app_id="chess"))
    await repo.install(_app(app_id="notes"))
    rows = await repo.list_installed()
    assert {r.app_id for r in rows} == {"chess", "notes"}


@pytest.mark.asyncio
async def test_set_enabled_and_uninstall(migrated_db):
    repo = SqliteAppRepo(migrated_db)
    await repo.install(_app())
    await repo.set_enabled("chess", enabled=False)
    assert (await repo.get("chess")).enabled is False
    await repo.uninstall("chess")
    assert await repo.get("chess") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/repositories/test_app_repo.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'socialhome.repositories.app_repo'`

- [ ] **Step 3: Write the implementation** (mirrors `preferences_repo.py`; note: app payload fields are **bound `?` parameters**, never f-string'd)

```python
# socialhome/repositories/app_repo.py
"""App-registry repo — the ``installed_apps`` table.

Wraps the SQL surface used by :class:`AppService` so the service depends
only on the abstract protocol. Mirrors the preferences-repo pattern.
Unlike preferences (which f-strings *column names* against an allow-list),
every value here — including the manifest JSON and the app id — is a
bound ``?`` parameter, so there is no injection surface to allow-list.
"""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.apps import AppManifest, InstalledApp


@runtime_checkable
class AbstractAppRepo(Protocol):
    async def list_installed(self) -> list[InstalledApp]: ...
    async def get(self, app_id: str) -> InstalledApp | None: ...
    async def install(self, app: InstalledApp) -> None: ...
    async def set_enabled(self, app_id: str, *, enabled: bool) -> None: ...
    async def uninstall(self, app_id: str) -> None: ...


def _row_to_app(row) -> InstalledApp:
    return InstalledApp(
        app_id=str(row["app_id"]),
        name=str(row["name"]),
        version=str(row["version"]),
        enabled=bool(row["enabled"]),
        manifest=AppManifest.from_dict(json.loads(row["manifest_json"])),
        bundle_path=str(row["bundle_path"]),
        bundle_sha256=str(row["bundle_sha256"]),
        source_url=str(row["source_url"]),
        installed_by=row["installed_by"],
        installed_at=str(row["installed_at"]),
    )


class SqliteAppRepo:
    """SQLite-backed :class:`AbstractAppRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def list_installed(self) -> list[InstalledApp]:
        rows = await self._db.fetchall(
            "SELECT * FROM installed_apps ORDER BY name COLLATE NOCASE",
        )
        return [_row_to_app(r) for r in rows]

    async def get(self, app_id: str) -> InstalledApp | None:
        row = await self._db.fetchone(
            "SELECT * FROM installed_apps WHERE app_id = ?",
            (app_id,),
        )
        return _row_to_app(row) if row is not None else None

    async def install(self, app: InstalledApp) -> None:
        await self._db.enqueue(
            """INSERT INTO installed_apps
                 (app_id, name, version, enabled, manifest_json, bundle_path,
                  bundle_sha256, source_url, installed_by, installed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                app.app_id, app.name, app.version, 1 if app.enabled else 0,
                json.dumps(
                    {
                        "entry": app.manifest.entry,
                        "icon": app.manifest.icon,
                        "capabilities": list(app.manifest.capabilities),
                    }
                ),
                app.bundle_path, app.bundle_sha256, app.source_url,
                app.installed_by, app.installed_at,
            ),
        )

    async def set_enabled(self, app_id: str, *, enabled: bool) -> None:
        await self._db.enqueue(
            "UPDATE installed_apps SET enabled = ? WHERE app_id = ?",
            (1 if enabled else 0, app_id),
        )

    async def uninstall(self, app_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM installed_apps WHERE app_id = ?",
            (app_id,),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/repositories/test_app_repo.py -v`
Expected: PASS (4 tests). If `migrated_db` isn't the fixture name, run `grep -rn "def .*db" tests/conftest.py` and use the real-SQLite fixture.

- [ ] **Step 5: Commit**

```bash
git add socialhome/repositories/app_repo.py tests/repositories/test_app_repo.py
git commit -m "feat: SqliteAppRepo for installed_apps"
```

---

## Task 4: Catalog service — fetch + parse remote `catalog.json`

**Files:**
- Create: `socialhome/services/app_catalog_service.py`
- Test: `tests/services/test_app_catalog_service.py`

- [ ] **Step 1: Write the failing test** (mock the HTTP at the `aiohttp.ClientSession` boundary — no real network, per CLAUDE.md unit-test rule)

```python
# tests/services/test_app_catalog_service.py
import json
import pytest
from socialhome.services.app_catalog_service import AppCatalogService


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def text(self): return json.dumps(self._payload)
    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _FakeSession:
    def __init__(self, payload): self._payload = payload
    def get(self, url, **kw): return _FakeResp(self._payload)


@pytest.mark.asyncio
async def test_fetch_catalog_parses_entries():
    payload = {"apps": [{
        "app_id": "chess", "name": "Chess", "latest_version": "1.0.0",
        "description": "Play chess", "icon_url": None,
        "capabilities": ["storage", "federation"],
        "bundle_url": "https://example/chess-1.0.0.tgz",
        "bundle_sha256": "cd" * 32,
    }]}
    svc = AppCatalogService(
        session_factory=lambda: _FakeSession(payload),
        catalog_url="https://example/catalog.json",
    )
    entries = await svc.fetch_catalog()
    assert len(entries) == 1
    assert entries[0].app_id == "chess"


@pytest.mark.asyncio
async def test_fetch_catalog_skips_malformed_entries():
    payload = {"apps": [{"app_id": "broken"}]}  # missing required fields
    svc = AppCatalogService(
        session_factory=lambda: _FakeSession(payload),
        catalog_url="https://example/catalog.json",
    )
    assert await svc.fetch_catalog() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/test_app_catalog_service.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
# socialhome/services/app_catalog_service.py
"""Fetch + parse the remote app catalog published by ``socialhome-apps``.

The catalog is a JSON document ``{"apps": [<AppCatalogEntry>, ...]}``
served from a GitHub release asset. This service only *reads* it; install
(download + verify + unpack) lives in :class:`AppService`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import aiohttp

from ..domain.apps import AppCatalogEntry

log = logging.getLogger(__name__)


class AppCatalogService:
    __slots__ = ("_session_factory", "_catalog_url")

    def __init__(
        self,
        *,
        session_factory: Callable[[], aiohttp.ClientSession],
        catalog_url: str,
    ) -> None:
        self._session_factory = session_factory
        self._catalog_url = catalog_url

    async def fetch_catalog(self) -> list[AppCatalogEntry]:
        """GET the catalog and parse it. Malformed entries are skipped
        (logged at WARNING) so one bad entry can't break Browse."""
        session = self._session_factory()
        async with session.get(self._catalog_url) as resp:
            resp.raise_for_status()
            raw = await resp.text()
        doc = json.loads(raw)
        out: list[AppCatalogEntry] = []
        for item in doc.get("apps", []):
            try:
                out.append(AppCatalogEntry.from_dict(item))
            except (ValueError, TypeError) as exc:
                log.warning("skipping malformed catalog entry %r: %s", item, exc)
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/test_app_catalog_service.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add socialhome/services/app_catalog_service.py tests/services/test_app_catalog_service.py
git commit -m "feat: AppCatalogService fetch+parse"
```

---

## Task 5: App service — install/uninstall/list/enable

**Files:**
- Create: `socialhome/services/app_service.py`
- Test: `tests/services/test_app_service.py`

**Design notes for the implementer:**
- Admin guard: raise `SpacePermissionError` (mapped to 403 by `routes/base.py:_iter`) when `actor_is_admin` is False — same pattern as `PreferencesService.update_household`.
- Download via `aiohttp`; **sha256 verify** the tarball bytes against the catalog entry before touching disk; **unpack** with `tarfile` inside `asyncio.to_thread` (CPU-bound, CLAUDE.md). Reject any tar member whose resolved path escapes the target dir (path-traversal guard) → `AppIntegrityError`.
- FS via `aiofiles.os` (`makedirs`, `path.isdir`); never sync `open`/`shutil` in `async def`.
- Bundle dir: `<media_path>/apps/<app_id>/<version>/`; store the **relative** `apps/<app_id>/<version>` as `bundle_path`.
- `installed_at` = `datetime.now(timezone.utc).isoformat()` (tz-aware, CLAUDE.md UTC rule).
- Publish `AppInstalled` / `AppUninstalled` bus events (add to `domain/events.py`) so the SPA refreshes — wrap in try/except + `log.debug`, like preferences.

- [ ] **Step 1: Write the failing test** (fake repo + fake catalog + fake downloader; assert verify + path-traversal rejection + admin guard)

```python
# tests/services/test_app_service.py
import hashlib
import pytest
from socialhome.domain.apps import (
    AppCatalogEntry, AppIntegrityError, AppManifest,
)
from socialhome.domain.space import SpacePermissionError
from socialhome.services.app_service import AppService


class _FakeRepo:
    def __init__(self): self.rows = {}
    async def list_installed(self): return list(self.rows.values())
    async def get(self, app_id): return self.rows.get(app_id)
    async def install(self, app): self.rows[app.app_id] = app
    async def set_enabled(self, app_id, *, enabled):
        a = self.rows[app_id]; self.rows[app_id] = a.__class__(**{**a.__dict__ if not hasattr(a, "__slots__") else {f: getattr(a, f) for f in a.__slots__}, "enabled": enabled})
    async def uninstall(self, app_id): self.rows.pop(app_id, None)


CHESS_TGZ = b"<<built in the test via tarfile>>"  # see helper below


def _catalog_entry(sha):
    return AppCatalogEntry(
        app_id="chess", name="Chess", latest_version="1.0.0",
        description="Play chess", icon_url=None,
        capabilities=("storage",), bundle_url="https://x/chess-1.0.0.tgz",
        bundle_sha256=sha,
    )


@pytest.mark.asyncio
async def test_install_requires_admin(tmp_path):
    svc = AppService(repo=_FakeRepo(), catalog=None, media_path=tmp_path,
                     downloader=lambda url: b"")
    with pytest.raises(SpacePermissionError):
        await svc.install("chess", actor_is_admin=False, actor_user_id="u1")


@pytest.mark.asyncio
async def test_install_rejects_sha_mismatch(tmp_path, chess_tarball):
    # chess_tarball fixture returns (bytes, sha256) of a valid bundle
    data, _real_sha = chess_tarball
    catalog = _StubCatalog([_catalog_entry("00" * 32)])  # wrong sha
    svc = AppService(repo=_FakeRepo(), catalog=catalog, media_path=tmp_path,
                     downloader=lambda url: data)
    with pytest.raises(AppIntegrityError):
        await svc.install("chess", actor_is_admin=True, actor_user_id="u1")


@pytest.mark.asyncio
async def test_install_unpacks_and_records(tmp_path, chess_tarball):
    data, sha = chess_tarball
    catalog = _StubCatalog([_catalog_entry(sha)])
    repo = _FakeRepo()
    svc = AppService(repo=repo, catalog=catalog, media_path=tmp_path,
                     downloader=lambda url: data)
    app = await svc.install("chess", actor_is_admin=True, actor_user_id="u1")
    assert app.app_id == "chess"
    assert (tmp_path / "apps" / "chess" / "1.0.0" / "index.html").exists()
    assert (await repo.get("chess")).bundle_sha256 == sha


class _StubCatalog:
    def __init__(self, entries): self._entries = entries
    async def fetch_catalog(self): return self._entries
```

Add to `tests/conftest.py` (or a local fixture) a `chess_tarball` fixture that builds a tiny `.tgz` containing `manifest.json` + `index.html` and returns `(bytes, sha256_hex)`. Also add a `test_install_rejects_path_traversal` case feeding a tarball with a `../evil` member and asserting `AppIntegrityError`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/test_app_service.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
# socialhome/services/app_service.py
"""AppService — install / uninstall / list / enable embedded apps.

Install pipeline: download tarball → sha256 verify → unpack (path-traversal
guarded, in a thread) → parse manifest → write registry row → publish event.
Admin-gated (raises SpacePermissionError, mapped to 403 by BaseView._iter).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import aiofiles.os

from ..domain.apps import (
    AppCatalogEntry,
    AppIntegrityError,
    AppManifest,
    AppNotFoundError,
    InstalledApp,
)
from ..domain.events import AppInstalled, AppUninstalled
from ..domain.space import SpacePermissionError
from ..repositories.app_repo import AbstractAppRepo
from .app_catalog_service import AppCatalogService

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from ..infrastructure.event_bus import EventBus

log = logging.getLogger(__name__)


class AppService:
    __slots__ = ("_repo", "_catalog", "_media_path", "_downloader", "_bus")

    def __init__(
        self,
        *,
        repo: AbstractAppRepo,
        catalog: AppCatalogService | None,
        media_path: Path,
        downloader: "Callable[[str], Awaitable[bytes] | bytes]",
        bus: "EventBus | None" = None,
    ) -> None:
        self._repo = repo
        self._catalog = catalog
        self._media_path = Path(media_path)
        self._downloader = downloader
        self._bus = bus

    async def list_installed(self) -> list[InstalledApp]:
        return await self._repo.list_installed()

    async def install(
        self, app_id: str, *, actor_is_admin: bool, actor_user_id: str,
    ) -> InstalledApp:
        if not actor_is_admin:
            raise SpacePermissionError("Only admins may install apps")
        if self._catalog is None:
            raise AppIntegrityError("no catalog configured")
        entry = await self._lookup_catalog_entry(app_id)
        data = await self._download(entry.bundle_url)
        actual = hashlib.sha256(data).hexdigest()
        if actual != entry.bundle_sha256:
            raise AppIntegrityError(
                f"sha256 mismatch for {app_id}: expected {entry.bundle_sha256}, got {actual}",
            )
        rel = f"apps/{app_id}/{entry.latest_version}"
        dest = self._media_path / rel
        manifest = await self._unpack_and_read_manifest(data, dest)
        app = InstalledApp(
            app_id=app_id, name=entry.name, version=entry.latest_version,
            enabled=True, manifest=manifest, bundle_path=rel,
            bundle_sha256=actual, source_url=entry.bundle_url,
            installed_by=actor_user_id,
            installed_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._repo.install(app)
        await self._publish(AppInstalled(app_id=app_id, name=entry.name))
        return app

    async def uninstall(self, app_id: str, *, actor_is_admin: bool) -> None:
        if not actor_is_admin:
            raise SpacePermissionError("Only admins may uninstall apps")
        app = await self._repo.get(app_id)
        if app is None:
            raise AppNotFoundError(app_id)
        await self._repo.uninstall(app_id)           # app_kv cascades (PR2)
        await self._remove_bundle(app.bundle_path)
        await self._publish(AppUninstalled(app_id=app_id))

    async def set_enabled(
        self, app_id: str, *, enabled: bool, actor_is_admin: bool,
    ) -> InstalledApp:
        if not actor_is_admin:
            raise SpacePermissionError("Only admins may enable/disable apps")
        if await self._repo.get(app_id) is None:
            raise AppNotFoundError(app_id)
        await self._repo.set_enabled(app_id, enabled=enabled)
        return await self._repo.get(app_id)  # type: ignore[return-value]

    # ── internals ────────────────────────────────────────────────────
    async def _lookup_catalog_entry(self, app_id: str) -> AppCatalogEntry:
        for entry in await self._catalog.fetch_catalog():  # type: ignore[union-attr]
            if entry.app_id == app_id:
                return entry
        raise AppNotFoundError(f"{app_id} not in catalog")

    async def _download(self, url: str) -> bytes:
        result = self._downloader(url)
        if hasattr(result, "__await__"):
            return await result  # type: ignore[misc]
        return result  # type: ignore[return-value]

    async def _unpack_and_read_manifest(self, data: bytes, dest: Path) -> AppManifest:
        await aiofiles.os.makedirs(dest, exist_ok=True)
        # tarfile + path checks are CPU/blocking → run in a thread.
        manifest_raw = await _to_thread_unpack(data, dest)
        return AppManifest.from_dict(json.loads(manifest_raw))

    async def _remove_bundle(self, rel: str) -> None:
        target = self._media_path / rel
        if await aiofiles.os.path.isdir(target):
            await _to_thread_rmtree(target)

    async def _publish(self, event) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.publish(event)
        except Exception as exc:  # pragma: no cover
            log.debug("app event publish failed: %s", exc)


def _to_thread_unpack(data: bytes, dest: Path) -> str:
    """Blocking: unpack tarball into *dest*, reject path traversal, return
    the raw manifest.json text. Run via asyncio.to_thread by the caller's
    helper below."""
    import asyncio
    return asyncio.get_event_loop().run_in_executor(None, _unpack_sync, data, dest)  # type: ignore[return-value]
```

> **Implementer note:** the two `_to_thread_*` shims above are sketched; replace with the canonical pattern used elsewhere in the repo — `return await asyncio.to_thread(self._unpack_sync, data, dest)` (see `services/backup_service.py:_read_restore_tar`). Implement `_unpack_sync(data, dest)` as: open `tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")`, for each member resolve `(dest / member.name).resolve()` and assert it's within `dest.resolve()` (else raise `AppIntegrityError`), `extractall(dest, members=safe)`, then read+return `(dest / "manifest.json").read_text()`. Implement `_rmtree` via `shutil.rmtree` inside `asyncio.to_thread`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/test_app_service.py -v`
Expected: PASS (admin guard, sha mismatch, unpack+record, path-traversal)

- [ ] **Step 5: Add the bus events**

In `socialhome/domain/events.py`, add (mirroring `HouseholdConfigChanged` shape):

```python
@dataclass(slots=True, frozen=True)
class AppInstalled:
    app_id: str
    name: str

@dataclass(slots=True, frozen=True)
class AppUninstalled:
    app_id: str
```

- [ ] **Step 6: Commit**

```bash
git add socialhome/services/app_service.py socialhome/domain/events.py tests/services/test_app_service.py tests/conftest.py
git commit -m "feat: AppService install/uninstall/enable with sha256 verify + safe unpack"
```

---

## Task 6: Wiring — app_keys + app.py

**Files:**
- Modify: `socialhome/app_keys.py`, `socialhome/app.py`

- [ ] **Step 1: Add the AppKey**

In `socialhome/app_keys.py` (next to `preferences_service_key` line 53):

```python
app_service_key: AppKey = AppKey("app_service")
```

- [ ] **Step 2: Wire the repo** — in `app.py` `_build_repos`, beside `preferences=SqlitePreferencesRepo(db),` add:

```python
        app=SqliteAppRepo(db),
```
and add the import `from .repositories.app_repo import SqliteAppRepo` next to the preferences-repo import (line ~119). Add `app` to the `Repos` container's field list (the same struct that declares `preferences`).

- [ ] **Step 3: Wire the service** — in `_build_services` (after the preferences-service block ~1417) add:

```python
    app_service = AppService(
        repo=repos.app,
        catalog=AppCatalogService(
            session_factory=lambda: aiohttp.ClientSession(),
            catalog_url=config.apps_catalog_url,   # add to config with a sane default
        ),
        media_path=config.media_path,
        downloader=_download_bytes,                # small aiohttp GET→bytes helper in app.py
        bus=bus,
    )
```
Import `AppService` / `AppCatalogService` at top of `app.py` next to `PreferencesService`. Register at the bottom beside `app[K.preferences_service_key] = preferences_service`:

```python
    app[K.app_service_key] = app_service
```
Add `apps_catalog_url` to `config.py` (default the official `socialhome-apps` releases catalog URL; document in `docs/architecture.md`).

- [ ] **Step 4: Verify the app still builds**

Run: `python -c "import socialhome.app"` then `pytest tests/test_app_factory.py -v` (or the existing create-app smoke test — `grep -rln "create_app" tests | head`)
Expected: PASS (app constructs with the new service registered)

- [ ] **Step 5: Commit**

```bash
git add socialhome/app_keys.py socialhome/app.py socialhome/config.py
git commit -m "feat: wire AppService + AppCatalogService into create_app"
```

---

## Task 7: Routes — `/api/apps`, `/api/apps/{app_id}`, `/api/apps/catalog`

**Files:**
- Create: `socialhome/routes/apps.py`
- Modify: `socialhome/routes/__init__.py`
- Test: `tests/routes/test_apps.py`

- [ ] **Step 1: Write the failing integration test** (real aiohttp `TestClient` + real SQLite — copy the client/admin fixtures another `tests/routes/test_*.py` uses)

```python
# tests/routes/test_apps.py
import pytest


@pytest.mark.asyncio
async def test_list_apps_empty(client, member_token):
    resp = await client.get("/api/apps", headers={"Authorization": f"Bearer {member_token}"})
    assert resp.status == 200
    assert (await resp.json()) == {"apps": []}


@pytest.mark.asyncio
async def test_install_requires_admin(client, member_token):
    resp = await client.post(
        "/api/apps", json={"app_id": "chess"},
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert resp.status == 403


@pytest.mark.asyncio
async def test_catalog_is_admin_only(client, member_token):
    resp = await client.get(
        "/api/apps/catalog",
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert resp.status == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/routes/test_apps.py -v`
Expected: FAIL with 404 (routes not registered yet)

- [ ] **Step 3: Write the routes**

```python
# socialhome/routes/apps.py
"""Social Home Apps registry routes.

* ``GET  /api/apps``            — list installed apps (members see enabled only).
* ``POST /api/apps``            — install from catalog (admin).
* ``GET  /api/apps/catalog``    — browse the remote catalog (admin).
* ``GET  /api/apps/{app_id}``   — one installed app.
* ``PATCH /api/apps/{app_id}``  — enable / disable (admin).
* ``DELETE /api/apps/{app_id}`` — uninstall (admin).
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import app_service_key
from ..domain.apps import AppNotFoundError
from ..security import error_response
from .base import BaseView


def _serialize(app) -> dict:
    return {
        "app_id": app.app_id,
        "name": app.name,
        "version": app.version,
        "enabled": app.enabled,
        "capabilities": list(app.manifest.capabilities),
        "icon": app.manifest.icon,
    }


class AppCollectionView(BaseView):
    async def get(self) -> web.Response:
        svc = self.svc(app_service_key)
        apps = await svc.list_installed()
        if not self.user.is_admin:
            apps = [a for a in apps if a.enabled]
        return self._json({"apps": [_serialize(a) for a in apps]})

    async def post(self) -> web.Response:
        if not self.user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        body = await self.body()
        app_id = body.get("app_id")
        if not isinstance(app_id, str) or not app_id:
            return error_response(400, "UNPROCESSABLE", "app_id is required.")
        svc = self.svc(app_service_key)
        app = await svc.install(
            app_id, actor_is_admin=True, actor_user_id=self.user.user_id,
        )
        return self._json(_serialize(app), status=201)


class AppCatalogView(BaseView):
    async def get(self) -> web.Response:
        if not self.user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        svc = self.svc(app_service_key)
        entries = await svc._catalog.fetch_catalog()  # expose via svc.browse_catalog()
        return self._json({"apps": [
            {
                "app_id": e.app_id, "name": e.name,
                "latest_version": e.latest_version, "description": e.description,
                "icon_url": e.icon_url, "capabilities": list(e.capabilities),
            }
            for e in entries
        ]})


class AppDetailView(BaseView):
    async def get(self) -> web.Response:
        svc = self.svc(app_service_key)
        app = await svc._repo.get(self.match("app_id"))  # add svc.get() wrapper
        if app is None or (not self.user.is_admin and not app.enabled):
            return error_response(404, "NOT_FOUND", "App not found.")
        return self._json(_serialize(app))

    async def patch(self) -> web.Response:
        if not self.user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        body = await self.body()
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return error_response(400, "UNPROCESSABLE", "enabled must be a boolean.")
        svc = self.svc(app_service_key)
        app = await svc.set_enabled(
            self.match("app_id"), enabled=enabled, actor_is_admin=True,
        )
        return self._json(_serialize(app))

    async def delete(self) -> web.Response:
        if not self.user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        svc = self.svc(app_service_key)
        await svc.uninstall(self.match("app_id"), actor_is_admin=True)
        return web.json_response({"status": "ok"})
```

> **Implementer note:** add thin `AppService.browse_catalog()` and `AppService.get(app_id)` wrappers so routes don't reach into `svc._catalog` / `svc._repo` (the `_`-prefixed reach-ins above are placeholders to be replaced). Map `AppNotFoundError` → 404 in `routes/base.py:_iter` (add to the import block + a `except AppNotFoundError` arm returning `error_response(404, "NOT_FOUND", str(exc))`).

- [ ] **Step 4: Register the routes** — in `routes/__init__.py`, add `from .apps import AppCollectionView, AppCatalogView, AppDetailView` (next to the `me_preferences` import) and, near the other `add_view` calls:

```python
    app.router.add_view("/api/apps", AppCollectionView)
    app.router.add_view("/api/apps/catalog", AppCatalogView)
    app.router.add_view("/api/apps/{app_id}", AppDetailView)
```
(Register `/api/apps/catalog` **before** `/api/apps/{app_id}` so "catalog" isn't captured as an app_id.)

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/routes/test_apps.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add socialhome/routes/apps.py socialhome/routes/__init__.py socialhome/routes/base.py tests/routes/test_apps.py
git commit -m "feat: /api/apps registry routes"
```

---

## Task 8: SPA — Browse/installed page + nav + dashboard card

**Files:**
- Create: `client/src/features/apps/AppsPage.tsx`, `client/src/store/apps.ts`
- Modify: `client/src/router.ts`, `client/src/components/SideNav.tsx`, `client/src/features/dashboard/DashboardPage.tsx`

- [ ] **Step 1: Add the store** — `client/src/store/apps.ts`: signals `installedApps`, `catalog`; `loadInstalled()` → `api.get('/api/apps')`, `loadCatalog()` → `api.get('/api/apps/catalog')`, `installApp(appId)` → `api.post('/api/apps', {app_id})`, `uninstallApp(appId)` → `api.delete('/api/apps/${appId}')`, `setEnabled(appId, enabled)` → `api.patch(...)`. Use the `api` singleton only (never `fetch('/api/...')`, per CLAUDE.md ingress rule).

- [ ] **Step 2: Add the route** — in `client/src/router.ts`:

```ts
'/apps': lazy(() => import('@/features/apps/AppsPage')),
```

- [ ] **Step 3: Add the page** — `AppsPage.tsx`: `useTitle('Apps')`; two sections — "Installed" grid (cards from installed apps with enable toggle + uninstall for admins) and, for admins, "Browse" (catalog entries with Install button + install-in-progress state + error toast). Reuse `Button`, `Modal` (confirm uninstall), `.sh-welcome-card` styling. Non-admins see installed enabled apps only (no Browse/manage controls).

- [ ] **Step 4: Add nav + dashboard entry** — in `SideNav.tsx` add `{ key: 'apps', label: 'Apps', href: '/apps', icon: 'apps' }` to `BROWSE_GROUP` (visible to all; install controls gate inside the page on `is_admin`). In `DashboardPage.tsx` add an "Apps" card listing up to ~4 installed enabled apps with an "Open Apps →" footer link.

- [ ] **Step 5: Type-check + unit test**

Run: `cd client && pnpm tsc --noEmit && pnpm vitest run src/store/apps.test.ts`
Expected: PASS. Add `src/store/apps.test.ts` mocking `api` to assert each store action hits the right path.

- [ ] **Step 6: Visual verification (CLAUDE.md — mandatory, both viewports)**

Boot `pnpm dev` + backend. Using the `chrome-devtools-mcp:chrome-devtools` skill, screenshot `/apps` at **1280×720** and **390×844**: installed grid, Browse list, install dialog, empty state ("No apps installed yet"), and a network-fail state. Walk the UX checklist (defaults, error/empty, discoverability, recovery, keyboard/focus rings). If the dev server is unreachable, say so in the PR instead of claiming verification.

- [ ] **Step 7: Commit**

```bash
git add client/src/features/apps client/src/store/apps.ts client/src/store/apps.test.ts client/src/router.ts client/src/components/SideNav.tsx client/src/features/dashboard/DashboardPage.tsx
git commit -m "feat: Apps browse/installed page + nav + dashboard card"
```

---

## Task 9: Docs

**Files:** `docs/api.md`, `docs/database.md`, `docs/architecture.md`, `docs/principles.md`

- [ ] **Step 1:** `docs/api.md` — add the `/api/apps`, `/api/apps/catalog`, `/api/apps/{app_id}` rows (methods, auth, admin-only flags); add a Rate limits row if you rate-limit install.
- [ ] **Step 2:** `docs/database.md` — under an "Apps" heading, the `installed_apps` table with the one-paragraph Purpose shape.
- [ ] **Step 3:** `docs/architecture.md` — a short "Social Home Apps" section (catalog-fetch sourcing, registry, where PR2–PR5 plug in).
- [ ] **Step 4:** `docs/principles.md` — **flag the §2 third-party-trust implication**: SH will execute fetched third-party JS (gated by sha256 pin + the PR3 sandbox). This is the explicit sign-off note CLAUDE.md requires; mention the full mitigation lands in PR3.
- [ ] **Step 5: Commit**

```bash
git add docs/
git commit -m "docs: Social Home Apps registry (api, database, architecture, principles)"
```

---

## Final verification (PR1)

- [ ] `pytest tests/domain/test_apps.py tests/repositories/test_app_repo.py tests/services/test_app_catalog_service.py tests/services/test_app_service.py tests/routes/test_apps.py -v` — all PASS.
- [ ] `pytest --cov=socialhome --cov-fail-under=90` — coverage gate holds.
- [ ] `cd client && pnpm tsc --noEmit && pnpm vitest run && pnpm build` — SPA clean.
- [ ] `ruff check socialhome && ruff format --check socialhome && mypy socialhome` — pre-commit hooks pass (no `--no-verify`).
- [ ] Manual: boot the app, point `apps_catalog_url` at a local static `catalog.json`, install a tiny test app, confirm it appears in `/apps` and on the dashboard, disable + uninstall it.
- [ ] PR label `feat`; PR body notes the §2 third-party-trust flag + screenshots (both viewports).

---

## Self-Review (writing-plans checklist — PR1)

- **Spec coverage:** registry table ✓ (T1), domain ✓ (T2), repo ✓ (T3), catalog fetch ✓ (T4), install/verify/unpack/uninstall/enable ✓ (T5), wiring ✓ (T6), routes + admin gating ✓ (T7), Browse/installed UI + dashboard ✓ (T8), docs incl. §2 flag ✓ (T9). Per-user KV, sandbox runtime, federation, chess demo are **out of PR1 scope by design** (PR2–PR5).
- **Placeholder scan:** two deliberate `_to_thread_*`/`svc._repo`/`svc._catalog` reach-ins are marked with **Implementer note** call-outs and the canonical replacement (`asyncio.to_thread` + thin service wrappers) named — resolve them during T5/T7, don't ship as-is.
- **Type consistency:** `InstalledApp`/`AppManifest`/`AppCatalogEntry` field names are identical across T2→T3→T5→T7; `AppService.install(app_id, *, actor_is_admin, actor_user_id)` signature matches its test and route call; `set_enabled(app_id, *, enabled, actor_is_admin)` consistent T5↔T7.

---

# Roadmap: PR2–PR5 (each gets its own writing-plans pass)

**PR2 — Storage API (per-user per app):** migration `0021_app_kv.sql` (`app_kv(app_id, user_id, key, value_json, updated_at)`, PK `(app_id,user_id,key)`, FK `app_id`→`installed_apps` `ON DELETE CASCADE`, FK `user_id`→`users` `ON DELETE CASCADE`); extend `app_repo` with `kv_get/kv_set/kv_list/kv_delete` (key is a **bound parameter**); quota guard in `app_service` (size + count caps → `AppQuotaExceededError` → 413/429); routes `GET/PUT/DELETE /api/apps/{app_id}/store/{key}` + `GET …/store` scoped to `self.user.user_id`; SDK `sh.store.*` (lands with PR3 bridge).

**PR3 — Sandbox runtime (the §2 sign-off PR):** `routes/app_bundle.py` `GET /api/apps/{app_id}/bundle/{tail:.+}` streaming from `media_path/apps/...` with `routes/media.py`-style path-traversal validation + **signed URL** (`?exp=&sig=` via `media_signer`, NOT bearer — works under haos no-token) + CSP `default-src 'none'; connect-src 'none'; script-src 'self' 'unsafe-inline'`; `features/apps/AppHost.tsx` renders `sandbox="allow-scripts"` (no `allow-same-origin` → opaque origin) iframe; `bridge.ts` `postMessage` RPC validated by `event.source === iframe.contentWindow` (origin is `"null"`), proxying `store.*` to the PR2 routes; a trivial local-only demo app proves the runtime. Extend `client/src/baseUrl.test.ts` for the bundle URL surface.

**PR4 — Federation `fed-app-v1` channel:** `federation/app_framing.py` (sibling of `media_framing.py`: `CHANNEL_LABEL="fed-app-v1"`, `[u8 frame_type][u32 hdr_len][hdr][u32 payload_len][payload]`, byte-exact signed envelope header, AES-256-GCM-sealed payload, `aead_suite` tag + `SUPPORTED_APP_AEAD_SUITES`); third up-front channel in `_RtcPeer.start_offer`/answerer path + `_drain_app_channel`; `APP_SESSION` event (open/accept/close on `fed-v1`) in `domain/federation.py`; `attach_apps` registering on `EventDispatchRegistry`; `AppFederationService` routing inbound to the installed app + pushing an `app.message` WS frame; **encryption-first**: `app_id`/`session_id`/`data` only inside ciphertext, no plaintext fallback; bump `OURS` 16→17 + `MIN_FOR_APP_CHANNEL=17` with history entry; gate session-open on `peer_supports(min_version=17)` (surface "peer must upgrade", no silent drop). Docs: new `docs/protocol/apps.md` (+ link from `docs/protocol/README.md`), `docs/protocol/capabilities.md` history row, `docs/crypto.md` app AEAD suite; update the `federation-demo` skill so `verify` round-trips an app session; run `pytest tests/protocol/ -m security`.

**PR5 — `socialhome-apps` repo + chess demo:** MPL-2.0 repo; `packages/app-sdk` (`@socialhome/app-sdk` wrapping the `postMessage` protocol: `sh.store.*`, `sh.federation.openSession/send/onMessage`, `sh.peers.list`, `sh.app.context`); `apps/chess` (manifest + UI + rules, stores board via `sh.store.set('game:{id}', …)`, plays moves over `fed-app-v1`); `.github/workflows/release.yml` building each app, computing `sha256`, and attaching `catalog.json` + per-app tarballs to the GitHub release; point a SH dev install's `apps_catalog_url` at it for the end-to-end demo.
