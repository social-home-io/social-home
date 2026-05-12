"""Tests for the SPA mount — ``socialhome.routes.spa``.

The Preact bundle lives in ``socialhome/static/`` at runtime; the
tests build their own throwaway tree in ``tmp_path`` and point
``mount_spa`` at it so we never depend on whether the repo has a
fresh ``pnpm --dir client run build``.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest
from aiohttp import web

from socialhome.app import create_app
from socialhome.config import Config
from socialhome.routes import spa as spa_module


@pytest.fixture
def fake_spa(tmp_path: Path) -> Path:
    """A minimal valid SPA tree the mount can pick up."""
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(
        '<!doctype html><head><base href="/" /><title>spa</title></head>'
    )
    (static / "manifest.json").write_text('{"name":"Social Home"}')
    (static / "sw.js").write_text("// service worker")
    (static / "favicon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    (static / "assets" / "app-deadbeef.js").write_text("console.log('app');")
    return static


@pytest.fixture
async def spa_client(aiohttp_client, tmp_dir, fake_spa, monkeypatch):
    """An app whose SPA mount points at ``fake_spa``."""
    monkeypatch.setattr(spa_module, "DEFAULT_STATIC_DIR", fake_spa)
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {
                "standalone": MappingProxyType(
                    {"external_url": "https://test.example"},
                ),
            },
        ),
    )
    app = create_app(cfg)
    return await aiohttp_client(app)


# ── Happy path ────────────────────────────────────────────────────────────


async def test_root_serves_index_html(spa_client):
    resp = await spa_client.get("/")
    assert resp.status == 200
    body = await resp.text()
    assert "<title>spa</title>" in body
    assert resp.headers["Cache-Control"] == "no-cache"


async def test_root_is_unauthenticated(spa_client):
    """Browser must be able to fetch the bundle before logging in."""
    resp = await spa_client.get("/")  # no Authorization header
    assert resp.status == 200


async def test_assets_are_served(spa_client):
    resp = await spa_client.get("/assets/app-deadbeef.js")
    assert resp.status == 200
    assert "console.log" in await resp.text()


async def test_manifest_json_served(spa_client):
    resp = await spa_client.get("/manifest.json")
    assert resp.status == 200
    body = await resp.json()
    assert body == {"name": "Social Home"}


async def test_service_worker_served_with_root_scope_header(spa_client):
    resp = await spa_client.get("/sw.js")
    assert resp.status == 200
    assert resp.headers["Service-Worker-Allowed"] == "/"
    assert resp.headers["Cache-Control"] == "no-cache"


async def test_favicon_svg_served_unauthenticated(spa_client):
    """``/favicon.svg`` is served without auth (browsers fetch it on every page)."""
    resp = await spa_client.get("/favicon.svg")
    assert resp.status == 200
    assert "image/svg" in resp.headers["Content-Type"]
    assert "<svg" in await resp.text()


# ── Backend routes stay backend routes ────────────────────────────────────


async def test_api_routes_not_shadowed_by_spa(spa_client):
    """``/api/me`` still 401s — the SPA mount must not touch /api/."""
    resp = await spa_client.get("/api/me")
    assert resp.status == 401
    assert resp.content_type == "application/json"


async def test_healthz_not_shadowed_by_spa(spa_client):
    resp = await spa_client.get("/healthz")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"


async def test_unknown_top_level_path_served_as_spa_shell(spa_client):
    """SPA catchall: any non-``/api/`` GET serves ``index.html`` so a
    hard refresh on a deep URL (``/feed``, ``/spaces/abc``, the
    ingress-prefixed ``/api/hassio_ingress/<token>/feed`` after HA
    Core strips the prefix) renders the SPA shell instead of 404.
    ``preact-iso`` then picks the right view client-side."""
    resp = await spa_client.get("/feed")
    assert resp.status == 200
    assert resp.content_type == "text/html"
    body = await resp.text()
    assert "<title>spa</title>" in body


# ── Missing-build fallback ────────────────────────────────────────────────


async def test_missing_static_dir_skips_mount(
    aiohttp_client, tmp_dir, tmp_path, monkeypatch, caplog
):
    """No ``socialhome/static/`` — log a warning, leave routes untouched."""
    missing = tmp_path / "no-static"  # doesn't exist
    monkeypatch.setattr(spa_module, "DEFAULT_STATIC_DIR", missing)
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {
                "standalone": MappingProxyType(
                    {"external_url": "https://test.example"},
                ),
            },
        ),
    )
    with caplog.at_level("WARNING", logger=spa_module.__name__):
        app = create_app(cfg)
    tc = await aiohttp_client(app)

    # /healthz still works, /api/me still 401s, / has no handler so 404.
    healthz = await tc.get("/healthz")
    assert healthz.status == 200
    me = await tc.get("/api/me")
    assert me.status == 401
    root = await tc.get("/")
    assert root.status == 404

    assert any("SPA bundle missing" in r.message for r in caplog.records)


async def test_mount_spa_returns_false_when_missing(tmp_path):
    app = web.Application()
    assert spa_module.mount_spa(app, static_dir=tmp_path / "absent") is False


async def test_mount_spa_returns_true_when_present(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("ok")
    app = web.Application()
    assert spa_module.mount_spa(app, static_dir=static) is True


# ── Ingress: <base href> substitution from X-Ingress-Path ──────────────────


async def test_root_base_href_defaults_to_slash(spa_client):
    """No ``X-Ingress-Path`` (standalone / HA-Core-direct) → ``<base href="/">``."""
    resp = await spa_client.get("/")
    body = await resp.text()
    assert '<base href="/">' in body


async def test_root_base_href_rewritten_from_ingress_path(spa_client):
    """Supervisor stamps the prefix into ``X-Ingress-Path``; the SPA's
    ``<base href>`` reflects it (trailing slash forced) so the SPA's
    relative URLs (``./api/me``, ``./api/ws``, …) resolve under the
    ingress-prefixed document URL."""
    resp = await spa_client.get(
        "/",
        headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"},
    )
    body = await resp.text()
    assert '<base href="/api/hassio_ingress/abc123/">' in body
    # Sanity: the placeholder is gone.
    assert '<base href="/">' not in body


async def test_root_base_href_strips_trailing_slash_from_header(spa_client):
    """If the header arrives with a trailing slash, we don't double up."""
    resp = await spa_client.get(
        "/",
        headers={"X-Ingress-Path": "/api/hassio_ingress/abc123/"},
    )
    body = await resp.text()
    assert '<base href="/api/hassio_ingress/abc123/">' in body
    assert '<base href="/api/hassio_ingress/abc123//">' not in body


async def test_root_substitution_logs_warning_when_placeholder_missing(
    spa_client, monkeypatch, caplog
):
    """If a future build drops the ``<base href>`` placeholder we log a
    warning and serve the HTML untouched — the SPA will still load."""
    static_dir = spa_client.app[spa_module._static_dir_key]
    (static_dir / "index.html").write_text(
        "<!doctype html><title>no placeholder</title>"
    )
    with caplog.at_level("WARNING", logger=spa_module.__name__):
        resp = await spa_client.get(
            "/",
            headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"},
        )
    body = await resp.text()
    assert resp.status == 200
    assert "<title>no placeholder</title>" in body
    assert any("no <base href> placeholder" in r.message for r in caplog.records)
