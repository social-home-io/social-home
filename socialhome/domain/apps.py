"""Social Home Apps — domain dataclasses for the app registry.

Pure data, no I/O. ``InstalledApp`` is the row shape of the
``installed_apps`` table; ``AppManifest`` is the parsed per-app
``manifest.json``; ``AppCatalogEntry`` is one item of the remote
``catalog.json`` published by the ``socialhome-apps`` repo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


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

    entry: str
    icon: str | None
    capabilities: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: dict) -> AppManifest:
        entry = data.get("entry")
        if not isinstance(entry, str) or not entry:
            raise ValueError("manifest.entry must be a non-empty string")
        if entry.startswith(("/", "..")):
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

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "app_id",
        "name",
        "latest_version",
        "description",
        "bundle_url",
        "bundle_sha256",
    )

    @classmethod
    def from_dict(cls, data: dict) -> AppCatalogEntry:
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
