"""Tests for GET /api/instance/config."""

from __future__ import annotations


async def test_instance_config_public_no_token_required(aiohttp_client, tmp_dir):
    """The endpoint must work BEFORE the SPA has a token."""
    from socialhome.app import create_app
    from socialhome.config import Config

    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "t.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
    )
    app = create_app(cfg)
    tc = await aiohttp_client(app)
    r = await tc.get("/api/instance/config")
    assert r.status == 200
    body = await r.json()
    assert body["mode"] == "standalone"
    assert body["setup_required"] is True
    assert "password_auth" in body["capabilities"]
    assert "instance_name" in body


async def test_instance_config_serialises_haos_capabilities(client):
    r = await client.get("/api/instance/config")
    assert r.status == 200
    body = await r.json()
    # The standalone test fixture wires standalone mode.
    assert body["mode"] == "standalone"
    assert body["capabilities"] == sorted(body["capabilities"])


async def test_instance_config_exposes_spa_bundle_hash_field(aiohttp_client, tmp_dir):
    """The response always carries ``spa_bundle_hash`` so the SPA's
    update-banner client can poll for changes. ``None`` when the
    backend isn't serving the SPA (dev mode), a string otherwise."""
    from socialhome.app import create_app
    from socialhome.config import Config

    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "t.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
    )
    app = create_app(cfg)
    tc = await aiohttp_client(app)
    r = await tc.get("/api/instance/config")
    body = await r.json()
    assert "spa_bundle_hash" in body
    # The field is either a non-empty string (real bundle on disk) or
    # ``None`` (no bundle in this dev / test setup).
    assert body["spa_bundle_hash"] is None or isinstance(body["spa_bundle_hash"], str)


async def test_instance_config_spa_bundle_hash_matches_bundle_when_mounted(
    aiohttp_client, tmp_dir, tmp_path, monkeypatch
):
    """When the SPA is mounted, the response carries the same hash that
    :func:`get_spa_bundle_hash` would extract from ``index.html`` —
    the contract the SPA client relies on for its stale-tab poll."""
    from socialhome.app import create_app
    from socialhome.config import Config
    from socialhome.routes import spa as spa_module

    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(
        '<!doctype html><base href="/" />'
        '<script type="module" src="./assets/index-DEADBEEF.js"></script>'
    )
    (static / "manifest.json").write_text('{"name":"test"}')
    (static / "sw.js").write_text("// sw")
    (static / "favicon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    (static / "assets" / "index-DEADBEEF.js").write_text("// app")
    monkeypatch.setattr(spa_module, "DEFAULT_STATIC_DIR", static)

    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "t.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
    )
    app = create_app(cfg)
    tc = await aiohttp_client(app)
    r = await tc.get("/api/instance/config")
    body = await r.json()
    assert body["spa_bundle_hash"] == "DEADBEEF"
