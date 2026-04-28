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
