"""AppService — install, uninstall, and enable/disable Social Home Apps.

Security invariants:
- SHA-256 is verified **before** any filesystem write.
- Tarball extraction rejects path traversal (``../`` or absolute paths),
  symlinks, and device/FIFO/block-device members.  Any violation raises
  :class:`AppIntegrityError` and the partially-extracted directory (if any)
  is cleaned up.
- CPU-bound work (sha256, tarfile extraction, shutil.rmtree) runs via
  ``asyncio.to_thread`` so the event loop is never blocked.
- Filesystem mutations use ``aiofiles.os`` for async makedirs; the tar
  extraction and rmtree happen inside the thread helper (plain pathlib there
  is intentional — we're already off the event loop).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
import json
import logging
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

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
    from ..infrastructure.event_bus import EventBus

log = logging.getLogger(__name__)


class AppService:
    """Install, uninstall, and toggle Social Home Apps.

    Parameters
    ----------
    repo:
        Persistent storage for installed-app rows.
    catalog:
        The catalog service used to browse available apps.  ``None`` means
        no catalog is configured — ``install`` and ``browse_catalog`` will
        signal the absence appropriately.
    media_path:
        Root directory for user-generated media.  App bundles are unpacked
        under ``<media_path>/apps/<app_id>/<version>/``.
    downloader:
        Callable ``(url: str) -> bytes | Awaitable[bytes]`` that fetches the
        raw bundle bytes.  Injected so unit tests never hit the network.
    bus:
        Optional event bus for publishing :class:`AppInstalled` /
        :class:`AppUninstalled`.  ``None`` silently skips publication.
    """

    __slots__ = ("_repo", "_catalog", "_media_path", "_downloader", "_bus")

    def __init__(
        self,
        *,
        repo: AbstractAppRepo,
        catalog: AppCatalogService | None,
        media_path: Path,
        downloader: Callable,
        bus: "EventBus | None" = None,
    ) -> None:
        self._repo = repo
        self._catalog = catalog
        self._media_path = Path(media_path)
        self._downloader = downloader
        self._bus = bus

    # ─── Read-side passthroughs ──────────────────────────────────────────────

    async def list_installed(self) -> list[InstalledApp]:
        """Return all installed apps."""
        return await self._repo.list_installed()

    async def get(self, app_id: str) -> InstalledApp | None:
        """Return a single installed app by id, or ``None``."""
        return await self._repo.get(app_id)

    async def browse_catalog(self) -> list[AppCatalogEntry]:
        """Return the remote catalog entries, or an empty list when no catalog
        is configured."""
        if self._catalog is None:
            return []
        return await self._catalog.fetch_catalog()

    # ─── Mutations ───────────────────────────────────────────────────────────

    async def install(
        self,
        app_id: str,
        *,
        actor_is_admin: bool,
        actor_user_id: str,
    ) -> InstalledApp:
        """Download, verify, unpack, and register an app.

        Steps:
        1. Admin gate.
        2. Catalog lookup → :class:`AppNotFoundError` if absent.
        3. Download bundle bytes via ``self._downloader``.
        4. SHA-256 verify; raise :class:`AppIntegrityError` on mismatch.
        5. Safe tar extraction into ``<media_path>/apps/<app_id>/<version>/``.
        6. Parse ``manifest.json`` from the unpacked dir.
        7. Build :class:`InstalledApp`, persist, publish :class:`AppInstalled`.
        """
        if not actor_is_admin:
            raise SpacePermissionError("Only admins may install apps")

        if self._catalog is None:
            raise AppIntegrityError("No app catalog configured")

        # Catalog lookup
        entries = await self.browse_catalog()
        entry = next((e for e in entries if e.app_id == app_id), None)
        if entry is None:
            raise AppNotFoundError(f"App {app_id!r} not found in catalog")

        # Download
        result = self._downloader(entry.bundle_url)
        if inspect.isawaitable(result):
            data: bytes = await result
        else:
            data = result

        # SHA-256 verify (CPU-bound) — must happen BEFORE any disk write
        actual_sha = await asyncio.to_thread(self._compute_sha256, data)
        if actual_sha != entry.bundle_sha256:
            raise AppIntegrityError(
                f"SHA-256 mismatch for {app_id!r}: "
                f"expected {entry.bundle_sha256!r}, got {actual_sha!r}"
            )

        # Destination directory (create parent asynchronously)
        dest = self._media_path / "apps" / app_id / entry.latest_version
        await aiofiles.os.makedirs(str(dest), exist_ok=True)

        # Safe extraction (CPU + I/O-bound; runs in thread).
        # _extract_bundle_sync also reads and returns the parsed manifest.json
        # dict so no blocking filesystem call is needed on the event loop.
        try:
            manifest_dict = await asyncio.to_thread(
                self._extract_bundle_sync, data, dest
            )
        except AppIntegrityError:
            # Clean up the partially-extracted directory before re-raising
            await asyncio.to_thread(self._rmtree_sync, dest)
            raise

        try:
            manifest = AppManifest.from_dict(manifest_dict)
        except (ValueError, KeyError, TypeError) as exc:
            await asyncio.to_thread(self._rmtree_sync, dest)
            raise AppIntegrityError(
                f"Invalid manifest.json in bundle for {app_id!r}: {exc}"
            ) from exc

        bundle_rel = f"apps/{app_id}/{entry.latest_version}"
        app = InstalledApp(
            app_id=app_id,
            name=entry.name,
            version=entry.latest_version,
            enabled=True,
            manifest=manifest,
            bundle_path=bundle_rel,
            bundle_sha256=actual_sha,
            source_url=entry.bundle_url,
            installed_by=actor_user_id,
            installed_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._repo.install(app)

        if self._bus is not None:
            try:
                await self._bus.publish(AppInstalled(app_id=app_id, name=entry.name))
            except Exception as exc:  # pragma: no cover
                log.debug("app_installed publish failed: %s", exc)

        return app

    async def uninstall(self, app_id: str, *, actor_is_admin: bool) -> None:
        """Remove an installed app from disk and the registry.

        Raises :class:`SpacePermissionError` for non-admins and
        :class:`AppNotFoundError` when the app is not installed.
        """
        if not actor_is_admin:
            raise SpacePermissionError("Only admins may uninstall apps")

        existing = await self._repo.get(app_id)
        if existing is None:
            raise AppNotFoundError(f"App {app_id!r} is not installed")

        await self._repo.uninstall(app_id)

        # Remove the bundle dir from disk (CPU/I/O-bound → thread)
        bundle_dir = self._media_path / existing.bundle_path
        if await aiofiles.os.path.isdir(str(bundle_dir)):
            await asyncio.to_thread(self._rmtree_sync, bundle_dir)

        if self._bus is not None:
            try:
                await self._bus.publish(AppUninstalled(app_id=app_id))
            except Exception as exc:  # pragma: no cover
                log.debug("app_uninstalled publish failed: %s", exc)

    async def set_enabled(
        self,
        app_id: str,
        *,
        enabled: bool,
        actor_is_admin: bool,
    ) -> InstalledApp:
        """Enable or disable an installed app.

        Raises :class:`SpacePermissionError` for non-admins and
        :class:`AppNotFoundError` when the app is not installed.
        Returns the updated :class:`InstalledApp`.
        """
        if not actor_is_admin:
            raise SpacePermissionError("Only admins may enable/disable apps")

        existing = await self._repo.get(app_id)
        if existing is None:
            raise AppNotFoundError(f"App {app_id!r} is not installed")

        await self._repo.set_enabled(app_id, enabled=enabled)

        updated = await self._repo.get(app_id)
        assert updated is not None  # just set it
        return updated

    # ─── Sync helpers (run inside asyncio.to_thread) ─────────────────────────

    @staticmethod
    def _compute_sha256(data: bytes) -> str:
        """Return the hex SHA-256 digest of ``data``.

        Runs inside ``asyncio.to_thread`` — blocking hashlib over potentially
        large buffers is too slow for the event loop.
        """
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _extract_bundle_sync(data: bytes, dest: Path) -> dict:
        """Extract a .tgz bundle into ``dest`` with path-traversal protection.

        Rejects:
        - Members with absolute paths (``name.startswith('/')``)
        - Members whose resolved path would escape ``dest`` (``../`` etc.)
        - Non-regular-file / non-directory members (symlinks, devices, FIFOs)

        After extraction, reads and returns the parsed ``manifest.json`` dict
        so the caller never needs a blocking filesystem read on the event loop.

        Raises :class:`AppIntegrityError` on any violation or a missing /
        invalid manifest.  Extraction is member-by-member so we validate
        *before* writing each file.
        """
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                for member in tar.getmembers():
                    # Reject symlinks, hard links, device files, FIFOs, etc.
                    if not (member.isfile() or member.isdir()):
                        raise AppIntegrityError(
                            f"Bundle contains non-regular member: {member.name!r} "
                            f"(type={member.type!r})"
                        )

                    # Reject absolute paths
                    if member.name.startswith("/"):
                        raise AppIntegrityError(
                            f"Bundle contains absolute path: {member.name!r}"
                        )

                    # Reject path traversal — resolve against dest and check
                    # that the result stays inside dest.
                    member_path = (dest / member.name).resolve()
                    try:
                        member_path.relative_to(dest.resolve())
                    except ValueError:
                        raise AppIntegrityError(
                            f"Bundle path traversal detected: {member.name!r} "
                            f"would escape destination {dest}"
                        )

                    # Safe to extract this member
                    if member.isdir():
                        member_path.mkdir(parents=True, exist_ok=True)
                    else:
                        member_path.parent.mkdir(parents=True, exist_ok=True)
                        fobj = tar.extractfile(member)
                        if fobj is not None:
                            member_path.write_bytes(fobj.read())
        except (tarfile.TarError, EOFError) as exc:
            raise AppIntegrityError(f"Corrupt or unreadable bundle: {exc}") from exc

        # Read manifest.json here (we're already off the event loop) so the
        # caller never needs a blocking Path.read_text on the event loop.
        manifest_path = dest / "manifest.json"
        try:
            manifest_text = manifest_path.read_text(encoding="utf-8")
            return json.loads(manifest_text)
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            raise AppIntegrityError(
                f"Invalid or missing manifest.json in bundle: {exc}"
            ) from exc

    @staticmethod
    def _rmtree_sync(path: Path) -> None:
        """Remove ``path`` and all its contents (synchronous, run in thread)."""
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
