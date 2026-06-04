"""App-registry repo — the ``installed_apps`` table.

Wraps the SQL surface used by :class:`AppService` so the service depends
only on the abstract protocol. Mirrors the preferences-repo pattern.

Unlike preferences (which f-strings *column names* against an allow-list),
every value here — including the manifest JSON and the app id — is a
bound ``?`` parameter, so there is no injection surface to allow-list.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.apps import AppKvEntry, AppManifest, AppPendingSession, InstalledApp

_PENDING_SESSION_TTL_SECONDS = 14 * 24 * 60 * 60  # 14 days


@runtime_checkable
class AbstractAppRepo(Protocol):
    async def list_installed(self) -> list[InstalledApp]: ...
    async def get(self, app_id: str) -> InstalledApp | None: ...
    async def install(self, app: InstalledApp) -> None: ...
    async def update_installed(self, app: InstalledApp) -> None: ...
    async def set_enabled(self, app_id: str, *, enabled: bool) -> None: ...
    async def set_min_age(self, app_id: str, min_age: int) -> None: ...
    async def uninstall(self, app_id: str) -> None: ...
    async def kv_get(
        self, app_id: str, user_id: str, key: str
    ) -> AppKvEntry | None: ...
    async def kv_list(self, app_id: str, user_id: str) -> list[AppKvEntry]: ...
    async def kv_set(
        self,
        app_id: str,
        user_id: str,
        key: str,
        value_json: str,
        updated_at: str,
    ) -> None: ...
    async def kv_delete(self, app_id: str, user_id: str, key: str) -> None: ...
    async def kv_count(self, app_id: str, user_id: str) -> int: ...
    async def add_pending_session(self, s: AppPendingSession) -> None: ...
    async def drain_pending_sessions(
        self,
        app_id: str,
        user_id: str,
        *,
        max_age_seconds: int = _PENDING_SESSION_TTL_SECONDS,
    ) -> list[AppPendingSession]: ...
    async def prune_pending_sessions(
        self, *, max_age_seconds: int = _PENDING_SESSION_TTL_SECONDS
    ) -> None: ...


def _row_to_kv(row) -> AppKvEntry:
    return AppKvEntry(
        app_id=str(row["app_id"]),
        user_id=str(row["user_id"]),
        key=str(row["key"]),
        value_json=str(row["value_json"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_pending(row) -> AppPendingSession:
    return AppPendingSession(
        app_id=str(row["app_id"]),
        user_id=str(row["user_id"]),
        session_id=str(row["session_id"]),
        from_instance=str(row["from_instance"]),
        from_user=row["from_user"],
        payload=json.loads(row["payload_json"]),
        created_at=str(row["created_at"]),
    )


def _row_to_app(row) -> InstalledApp:
    # min_age column added in migration 0022; default to 0 if absent (safety).
    try:
        min_age = int(row["min_age"] or 0)
    except KeyError, TypeError, ValueError:
        min_age = 0
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
        min_age=min_age,
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
                  bundle_sha256, source_url, installed_by, installed_at, min_age)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                app.min_age,
            ),
        )

    async def update_installed(self, app: InstalledApp) -> None:
        await self._db.enqueue(
            """UPDATE installed_apps
               SET name = ?, version = ?, manifest_json = ?,
                   bundle_path = ?, bundle_sha256 = ?, source_url = ?,
                   min_age = ?
               WHERE app_id = ?""",
            (
                app.name,
                app.version,
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
                app.min_age,
                app.app_id,
            ),
        )

    async def set_enabled(self, app_id: str, *, enabled: bool) -> None:
        await self._db.enqueue(
            "UPDATE installed_apps SET enabled = ? WHERE app_id = ?",
            (1 if enabled else 0, app_id),
        )

    async def set_min_age(self, app_id: str, min_age: int) -> None:
        await self._db.enqueue(
            "UPDATE installed_apps SET min_age = ? WHERE app_id = ?",
            (min_age, app_id),
        )

    async def uninstall(self, app_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM installed_apps WHERE app_id = ?",
            (app_id,),
        )

    async def kv_get(self, app_id: str, user_id: str, key: str) -> AppKvEntry | None:
        row = await self._db.fetchone(
            "SELECT * FROM app_kv WHERE app_id = ? AND user_id = ? AND key = ?",
            (app_id, user_id, key),
        )
        return _row_to_kv(row) if row is not None else None

    async def kv_list(self, app_id: str, user_id: str) -> list[AppKvEntry]:
        rows = await self._db.fetchall(
            "SELECT * FROM app_kv WHERE app_id = ? AND user_id = ? ORDER BY key",
            (app_id, user_id),
        )
        return [_row_to_kv(r) for r in rows]

    async def kv_set(
        self,
        app_id: str,
        user_id: str,
        key: str,
        value_json: str,
        updated_at: str,
    ) -> None:
        await self._db.enqueue(
            """INSERT INTO app_kv(app_id, user_id, key, value_json, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(app_id, user_id, key)
               DO UPDATE SET value_json = excluded.value_json,
                             updated_at = excluded.updated_at""",
            (app_id, user_id, key, value_json, updated_at),
        )

    async def kv_delete(self, app_id: str, user_id: str, key: str) -> None:
        await self._db.enqueue(
            "DELETE FROM app_kv WHERE app_id = ? AND user_id = ? AND key = ?",
            (app_id, user_id, key),
        )

    async def kv_count(self, app_id: str, user_id: str) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS cnt FROM app_kv WHERE app_id = ? AND user_id = ?",
            (app_id, user_id),
        )
        return int(row["cnt"]) if row is not None else 0

    # ── Pending-session invites ──────────────────────────────────────────

    async def add_pending_session(self, s: AppPendingSession) -> None:
        await self._db.enqueue(
            """INSERT OR REPLACE INTO app_pending_sessions
                 (app_id, user_id, session_id, from_instance, from_user,
                  payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                s.app_id,
                s.user_id,
                s.session_id,
                s.from_instance,
                s.from_user,
                json.dumps(s.payload),
                s.created_at,
            ),
        )

    async def drain_pending_sessions(
        self,
        app_id: str,
        user_id: str,
        *,
        max_age_seconds: int = _PENDING_SESSION_TTL_SECONDS,
    ) -> list[AppPendingSession]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        ).isoformat()
        rows = await self._db.fetchall(
            """SELECT * FROM app_pending_sessions
               WHERE app_id = ? AND user_id = ? AND created_at >= ?
               ORDER BY created_at ASC""",
            (app_id, user_id, cutoff),
        )
        # Drain everything for the pair — both the fresh rows we return and any
        # expired ones — in one delete.
        await self._db.enqueue(
            "DELETE FROM app_pending_sessions WHERE app_id = ? AND user_id = ?",
            (app_id, user_id),
        )
        return [_row_to_pending(r) for r in rows]

    async def prune_pending_sessions(
        self, *, max_age_seconds: int = _PENDING_SESSION_TTL_SECONDS
    ) -> None:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        ).isoformat()
        await self._db.enqueue(
            "DELETE FROM app_pending_sessions WHERE created_at < ?",
            (cutoff,),
        )
