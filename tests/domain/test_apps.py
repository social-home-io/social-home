"""Tests for app-registry domain dataclasses."""

from __future__ import annotations

import dataclasses

import pytest

from socialhome.domain.apps import (
    AppAlreadyInstalledError,
    AppError,
    AppIntegrityError,
    AppKvEntry,
    AppManifest,
    AppNotEnabledError,
    AppNotFoundError,
    AppQuotaExceededError,
    AppCatalogEntry,
    InstalledApp,
)


def test_installed_app_is_frozen():
    app = InstalledApp(
        app_id="chess",
        name="Chess",
        version="1.0.0",
        enabled=True,
        manifest=AppManifest(entry="index.html", icon=None, capabilities=("storage",)),
        bundle_path="apps/chess/1.0.0",
        bundle_sha256="ab" * 32,
        source_url="https://example/chess-1.0.0.tgz",
        installed_by="u1",
        installed_at="2026-06-02T00:00:00+00:00",
    )
    assert app.app_id == "chess"
    with pytest.raises(dataclasses.FrozenInstanceError):
        app.app_id = "other"  # type: ignore[misc]


def test_catalog_entry_from_dict_validates_required_fields():
    entry = AppCatalogEntry.from_dict(
        {
            "app_id": "chess",
            "name": "Chess",
            "latest_version": "1.0.0",
            "description": "Play chess",
            "icon_url": None,
            "capabilities": ["storage", "federation"],
            "bundle_url": "https://example/chess-1.0.0.tgz",
            "bundle_sha256": "cd" * 32,
        }
    )
    assert entry.app_id == "chess"
    assert entry.bundle_sha256 == "cd" * 32


def test_catalog_entry_from_dict_missing_fields():
    with pytest.raises(ValueError):
        AppCatalogEntry.from_dict({"app_id": "x"})  # missing fields


def test_app_not_found_is_exception_subclass():
    assert issubclass(AppNotFoundError, Exception)
    assert issubclass(AppNotEnabledError, Exception)
    assert issubclass(AppAlreadyInstalledError, Exception)
    assert issubclass(AppIntegrityError, Exception)


def test_app_manifest_from_dict_validates_entry():
    manifest = AppManifest.from_dict(
        {
            "entry": "index.html",
            "icon": "icon.svg",
            "capabilities": ["storage", "federation"],
        }
    )
    assert manifest.entry == "index.html"
    assert manifest.icon == "icon.svg"
    assert manifest.capabilities == ("storage", "federation")


def test_app_manifest_from_dict_rejects_absolute_path():
    with pytest.raises(ValueError, match="relative path"):
        AppManifest.from_dict({"entry": "/index.html"})


def test_app_manifest_from_dict_rejects_parent_traversal():
    with pytest.raises(ValueError, match="relative path"):
        AppManifest.from_dict({"entry": "../index.html"})


def test_app_manifest_from_dict_requires_entry():
    with pytest.raises(ValueError, match="entry must be"):
        AppManifest.from_dict({})


def test_app_kv_entry_is_frozen():
    e = AppKvEntry(
        app_id="chess",
        user_id="u1",
        key="game:1",
        value_json='{"turn":"w"}',
        updated_at="2026-06-02T00:00:00+00:00",
    )
    assert e.key == "game:1"
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.key = "x"  # type: ignore[misc]


def test_app_quota_exceeded_is_app_error():
    assert issubclass(AppQuotaExceededError, AppError)
