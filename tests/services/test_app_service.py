"""Tests for AppService — install/uninstall/enable with sha256 verify + safe unpack.

Security invariants tested:
- SHA-256 mismatch → AppIntegrityError, nothing written to disk or repo.
- Path traversal in tar member → AppIntegrityError, no file escapes the dest dir.
- Symlink member → AppIntegrityError (non-regular-file/non-dir rejected).
- Absolute path member → AppIntegrityError.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest

from socialhome.domain.apps import (
    AppCatalogEntry,
    AppIntegrityError,
    AppNotFoundError,
    InstalledApp,
)
from socialhome.domain.events import AppInstalled, AppUninstalled
from socialhome.domain.space import SpacePermissionError
from socialhome.services.app_service import AppService


# ─── Helpers ─────────────────────────────────────────────────────────────────


def chess_tarball() -> tuple[bytes, str]:
    """Build a minimal in-memory .tgz for the chess app.

    Returns ``(bytes, sha256_hex)``.  The archive contains:
    - ``manifest.json`` — valid manifest with entry=index.html
    - ``index.html``    — placeholder page body
    """
    buf = io.BytesIO()
    manifest = json.dumps({"entry": "index.html", "capabilities": ["storage"]}).encode()
    html = b"<html><body>Chess</body></html>"

    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w|") as tar:
            for name, data in [("manifest.json", manifest), ("index.html", html)]:
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

    data = buf.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    return data, digest


def make_tarball_with_member(name: str, data: bytes = b"evil") -> tuple[bytes, str]:
    """Build a .tgz containing ``manifest.json`` plus a single member ``name``."""
    buf = io.BytesIO()
    manifest = json.dumps({"entry": "index.html", "capabilities": []}).encode()
    html = b"ok"

    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w|") as tar:
            # valid manifest
            info = tarfile.TarInfo(name="manifest.json")
            info.size = len(manifest)
            tar.addfile(info, io.BytesIO(manifest))
            # valid index.html
            info2 = tarfile.TarInfo(name="index.html")
            info2.size = len(html)
            tar.addfile(info2, io.BytesIO(html))
            # the adversarial member
            info3 = tarfile.TarInfo(name=name)
            info3.size = len(data)
            tar.addfile(info3, io.BytesIO(data))

    raw = buf.getvalue()
    return raw, hashlib.sha256(raw).hexdigest()


def make_symlink_tarball() -> tuple[bytes, str]:
    """Build a .tgz containing a symlink member (type SYMTYPE)."""
    buf = io.BytesIO()
    manifest = json.dumps({"entry": "index.html", "capabilities": []}).encode()
    html = b"ok"

    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w|") as tar:
            info = tarfile.TarInfo(name="manifest.json")
            info.size = len(manifest)
            tar.addfile(info, io.BytesIO(manifest))
            info2 = tarfile.TarInfo(name="index.html")
            info2.size = len(html)
            tar.addfile(info2, io.BytesIO(html))
            # symlink member
            sym = tarfile.TarInfo(name="evil_link")
            sym.type = tarfile.SYMTYPE
            sym.linkname = "/etc/passwd"
            sym.size = 0
            tar.addfile(sym, io.BytesIO(b""))

    raw = buf.getvalue()
    return raw, hashlib.sha256(raw).hexdigest()


# ─── Fakes ────────────────────────────────────────────────────────────────────


class _FakeAppRepo:
    """Dict-backed in-memory AppRepo."""

    def __init__(self) -> None:
        self._apps: dict[str, InstalledApp] = {}

    async def list_installed(self) -> list[InstalledApp]:
        return list(self._apps.values())

    async def get(self, app_id: str) -> InstalledApp | None:
        return self._apps.get(app_id)

    async def install(self, app: InstalledApp) -> None:
        self._apps[app.app_id] = app

    async def set_enabled(self, app_id: str, *, enabled: bool) -> None:
        existing = self._apps[app_id]
        # InstalledApp is frozen — rebuild with new enabled flag
        self._apps[app_id] = InstalledApp(
            app_id=existing.app_id,
            name=existing.name,
            version=existing.version,
            enabled=enabled,
            manifest=existing.manifest,
            bundle_path=existing.bundle_path,
            bundle_sha256=existing.bundle_sha256,
            source_url=existing.source_url,
            installed_by=existing.installed_by,
            installed_at=existing.installed_at,
        )

    async def uninstall(self, app_id: str) -> None:
        self._apps.pop(app_id, None)


class _StubCatalog:
    """Minimal stub that wraps a list[AppCatalogEntry]."""

    def __init__(self, entries: list[AppCatalogEntry]) -> None:
        self._entries = entries

    async def fetch_catalog(self) -> list[AppCatalogEntry]:
        return list(self._entries)


class _RecordingBus:
    """Captures published events for assertion."""

    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.published.append(event)


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def chess_bundle() -> tuple[bytes, str]:
    return chess_tarball()


@pytest.fixture()
def catalog_entry(chess_bundle: tuple[bytes, str]) -> AppCatalogEntry:
    _, sha = chess_bundle
    return AppCatalogEntry(
        app_id="chess",
        name="Chess",
        latest_version="1.0.0",
        description="Play chess",
        icon_url=None,
        capabilities=("storage",),
        bundle_url="https://example.com/chess-1.0.0.tgz",
        bundle_sha256=sha,
    )


def make_service(
    tmp_path: Path,
    entries: list[AppCatalogEntry],
    bundle_bytes: bytes,
    *,
    bus: _RecordingBus | None = None,
) -> tuple[AppService, _FakeAppRepo]:
    repo = _FakeAppRepo()
    stub_catalog = _StubCatalog(entries)

    async def downloader(url: str) -> bytes:
        return bundle_bytes

    svc = AppService(
        repo=repo,
        catalog=stub_catalog,  # type: ignore[arg-type]
        media_path=tmp_path,
        downloader=downloader,
        bus=bus,
    )
    return svc, repo


# ─── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_requires_admin(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """install() with actor_is_admin=False must raise SpacePermissionError."""
    bundle_bytes, _ = chess_bundle
    svc, repo = make_service(tmp_path, [catalog_entry], bundle_bytes)

    with pytest.raises(SpacePermissionError):
        await svc.install(
            "chess",
            actor_is_admin=False,
            actor_user_id="user1",
        )

    # Nothing written to repo
    assert await repo.list_installed() == []


@pytest.mark.asyncio
async def test_install_rejects_sha_mismatch(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """install() must raise AppIntegrityError if sha256 doesn't match.

    Assert that NOTHING is written to disk or repo — the integrity check must
    happen BEFORE any filesystem mutation.
    """
    bundle_bytes, _ = chess_bundle
    # Catalog says a DIFFERENT sha than the actual bundle
    bad_entry = AppCatalogEntry(
        app_id=catalog_entry.app_id,
        name=catalog_entry.name,
        latest_version=catalog_entry.latest_version,
        description=catalog_entry.description,
        icon_url=catalog_entry.icon_url,
        capabilities=catalog_entry.capabilities,
        bundle_url=catalog_entry.bundle_url,
        bundle_sha256="a" * 64,  # wrong hash
    )
    svc, repo = make_service(tmp_path, [bad_entry], bundle_bytes)

    with pytest.raises(AppIntegrityError):
        await svc.install(
            "chess",
            actor_is_admin=True,
            actor_user_id="admin1",
        )

    # Nothing in repo
    assert await repo.list_installed() == []
    # Nothing on disk
    assert not (tmp_path / "apps" / "chess").exists()


@pytest.mark.asyncio
async def test_install_unpacks_and_records(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """Happy path: valid bundle → file on disk + repo row with verified sha."""
    bundle_bytes, expected_sha = chess_bundle
    bus = _RecordingBus()
    svc, repo = make_service(tmp_path, [catalog_entry], bundle_bytes, bus=bus)

    app = await svc.install(
        "chess",
        actor_is_admin=True,
        actor_user_id="admin1",
    )

    # File on disk
    index_path = tmp_path / "apps" / "chess" / "1.0.0" / "index.html"
    assert index_path.exists(), f"expected {index_path} to exist"

    # Repo row
    installed = await repo.get("chess")
    assert installed is not None
    assert installed.bundle_sha256 == expected_sha
    assert installed.enabled is True
    assert installed.app_id == "chess"
    assert installed.bundle_path == "apps/chess/1.0.0"

    # Return value
    assert app.app_id == "chess"

    # Event published
    assert len(bus.published) == 1
    evt = bus.published[0]
    assert isinstance(evt, AppInstalled)
    assert evt.app_id == "chess"
    assert evt.name == "Chess"


@pytest.mark.asyncio
async def test_install_rejects_path_traversal(
    tmp_path: Path,
) -> None:
    """A tarball member named ``../evil.txt`` must raise AppIntegrityError.

    The evil file must NOT be created outside the destination directory.
    """
    raw, sha = make_tarball_with_member("../evil.txt")
    entry = AppCatalogEntry(
        app_id="chess",
        name="Chess",
        latest_version="1.0.0",
        description="x",
        icon_url=None,
        capabilities=(),
        bundle_url="https://example.com/chess-1.0.0.tgz",
        bundle_sha256=sha,
    )
    svc, repo = make_service(tmp_path, [entry], raw)

    with pytest.raises(AppIntegrityError):
        await svc.install(
            "chess",
            actor_is_admin=True,
            actor_user_id="admin1",
        )

    # The evil file must NOT exist outside the dest dir
    evil_path = tmp_path / "evil.txt"
    assert not evil_path.exists(), (
        f"path traversal succeeded — {evil_path} was created!"
    )

    # Nothing in repo
    assert await repo.list_installed() == []


@pytest.mark.asyncio
async def test_install_rejects_absolute_path_member(
    tmp_path: Path,
) -> None:
    """A tarball member with an absolute path must raise AppIntegrityError."""
    raw, sha = make_tarball_with_member("/etc/evil.txt")
    entry = AppCatalogEntry(
        app_id="chess",
        name="Chess",
        latest_version="1.0.0",
        description="x",
        icon_url=None,
        capabilities=(),
        bundle_url="https://example.com/chess-1.0.0.tgz",
        bundle_sha256=sha,
    )
    svc, repo = make_service(tmp_path, [entry], raw)

    with pytest.raises(AppIntegrityError):
        await svc.install(
            "chess",
            actor_is_admin=True,
            actor_user_id="admin1",
        )

    assert await repo.list_installed() == []


@pytest.mark.asyncio
async def test_install_rejects_symlink_member(
    tmp_path: Path,
) -> None:
    """A tarball containing a symlink member must raise AppIntegrityError."""
    raw, sha = make_symlink_tarball()
    entry = AppCatalogEntry(
        app_id="chess",
        name="Chess",
        latest_version="1.0.0",
        description="x",
        icon_url=None,
        capabilities=(),
        bundle_url="https://example.com/chess-1.0.0.tgz",
        bundle_sha256=sha,
    )
    svc, repo = make_service(tmp_path, [entry], raw)

    with pytest.raises(AppIntegrityError):
        await svc.install(
            "chess",
            actor_is_admin=True,
            actor_user_id="admin1",
        )

    assert await repo.list_installed() == []


@pytest.mark.asyncio
async def test_uninstall_removes_row_and_bundle(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """After install, uninstall deletes the repo row and the bundle dir."""
    bundle_bytes, _ = chess_bundle
    bus = _RecordingBus()
    svc, repo = make_service(tmp_path, [catalog_entry], bundle_bytes, bus=bus)

    # First install
    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")
    bundle_dir = tmp_path / "apps" / "chess" / "1.0.0"
    assert bundle_dir.exists()

    # Now uninstall
    await svc.uninstall("chess", actor_is_admin=True)

    # Row gone
    assert await repo.get("chess") is None

    # Bundle dir gone
    assert not bundle_dir.exists()

    # AppUninstalled event published (it's the second event in the bus)
    uninstall_events = [e for e in bus.published if isinstance(e, AppUninstalled)]
    assert len(uninstall_events) == 1
    assert uninstall_events[0].app_id == "chess"


@pytest.mark.asyncio
async def test_uninstall_requires_admin(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """uninstall() with actor_is_admin=False must raise SpacePermissionError."""
    bundle_bytes, _ = chess_bundle
    svc, repo = make_service(tmp_path, [catalog_entry], bundle_bytes)

    # Install first as admin
    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")

    with pytest.raises(SpacePermissionError):
        await svc.uninstall("chess", actor_is_admin=False)

    # Still installed
    assert await repo.get("chess") is not None


@pytest.mark.asyncio
async def test_uninstall_missing_raises_not_found(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """Uninstalling a non-existent app raises AppNotFoundError."""
    bundle_bytes, _ = chess_bundle
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)

    with pytest.raises(AppNotFoundError):
        await svc.uninstall("nonexistent", actor_is_admin=True)


@pytest.mark.asyncio
async def test_set_enabled_toggles(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """set_enabled can disable then re-enable an installed app."""
    bundle_bytes, _ = chess_bundle
    svc, repo = make_service(tmp_path, [catalog_entry], bundle_bytes)

    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")

    # Disable
    result = await svc.set_enabled("chess", enabled=False, actor_is_admin=True)
    assert result.enabled is False
    assert (await repo.get("chess")).enabled is False  # type: ignore[union-attr]

    # Re-enable
    result = await svc.set_enabled("chess", enabled=True, actor_is_admin=True)
    assert result.enabled is True
    assert (await repo.get("chess")).enabled is True  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_set_enabled_requires_admin(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """set_enabled() with actor_is_admin=False must raise SpacePermissionError."""
    bundle_bytes, _ = chess_bundle
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)

    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")

    with pytest.raises(SpacePermissionError):
        await svc.set_enabled("chess", enabled=False, actor_is_admin=False)


@pytest.mark.asyncio
async def test_set_enabled_missing_raises_not_found(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """set_enabled on non-existent app raises AppNotFoundError."""
    bundle_bytes, _ = chess_bundle
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)

    with pytest.raises(AppNotFoundError):
        await svc.set_enabled("nonexistent", enabled=False, actor_is_admin=True)


@pytest.mark.asyncio
async def test_install_no_catalog_raises_integrity_error(
    tmp_path: Path,
    chess_bundle: tuple[bytes, str],
) -> None:
    """install() with catalog=None must raise AppIntegrityError."""
    bundle_bytes, _ = chess_bundle
    repo = _FakeAppRepo()

    async def downloader(url: str) -> bytes:
        return bundle_bytes

    svc = AppService(
        repo=repo,
        catalog=None,
        media_path=tmp_path,
        downloader=downloader,
    )

    with pytest.raises(AppIntegrityError):
        await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")


@pytest.mark.asyncio
async def test_install_app_id_not_in_catalog(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """install() with unknown app_id raises AppNotFoundError."""
    bundle_bytes, _ = chess_bundle
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)

    with pytest.raises(AppNotFoundError):
        await svc.install("unknown_app", actor_is_admin=True, actor_user_id="admin1")


@pytest.mark.asyncio
async def test_list_installed_passthrough(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """list_installed() returns the repo's list."""
    bundle_bytes, _ = chess_bundle
    svc, repo = make_service(tmp_path, [catalog_entry], bundle_bytes)

    assert await svc.list_installed() == []
    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")
    result = await svc.list_installed()
    assert len(result) == 1
    assert result[0].app_id == "chess"


@pytest.mark.asyncio
async def test_browse_catalog_passthrough(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """browse_catalog() delegates to the catalog service."""
    bundle_bytes, _ = chess_bundle
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)

    entries = await svc.browse_catalog()
    assert len(entries) == 1
    assert entries[0].app_id == "chess"


@pytest.mark.asyncio
async def test_browse_catalog_no_catalog_returns_empty(
    tmp_path: Path,
) -> None:
    """browse_catalog() with catalog=None returns empty list."""
    repo = _FakeAppRepo()

    async def downloader(url: str) -> bytes:
        return b""

    svc = AppService(
        repo=repo,
        catalog=None,
        media_path=tmp_path,
        downloader=downloader,
    )
    assert await svc.browse_catalog() == []
