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
                app.app_id,
                app.name,
                app.version,
                1 if app.enabled else 0,
                json.dumps(
                    {
                        "entry": app.manifest.entry,
                        "icon": app.manifest.icon,
                        "capabilities": list(app.manifest.capabilities),
                    }
                ),
                app.bundle_path,
                app.bundle_sha256,
                app.source_url,
                app.installed_by,
                app.installed_at,
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
