"""Tests for AppCatalogService."""

import json
from typing import Any

import pytest

from socialhome.services.app_catalog_service import AppCatalogService


class _FakeResp:
    """Fake aiohttp response for testing."""

    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status = status

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    async def text(self) -> str:
        return json.dumps(self._payload)

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _FakeSession:
    """Fake aiohttp ClientSession for testing."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def get(self, url: str, **kw: Any) -> _FakeResp:
        return _FakeResp(self._payload)


@pytest.mark.asyncio
async def test_fetch_catalog_parses_entries():
    """fetch_catalog should parse valid entries."""
    payload = {
        "apps": [
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
        ]
    }
    svc = AppCatalogService(
        session_factory=lambda: _FakeSession(payload),
        catalog_url="https://example/catalog.json",
    )
    entries = await svc.fetch_catalog()
    assert len(entries) == 1
    assert entries[0].app_id == "chess"


@pytest.mark.asyncio
async def test_fetch_catalog_skips_malformed_entries():
    """fetch_catalog should skip entries that fail from_dict."""
    payload = {"apps": [{"app_id": "broken"}]}  # missing required fields
    svc = AppCatalogService(
        session_factory=lambda: _FakeSession(payload),
        catalog_url="https://example/catalog.json",
    )
    assert await svc.fetch_catalog() == []
