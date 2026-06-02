"""Tests for socialhome.app — create_app() factory and startup hook."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from socialhome.app import create_app
from socialhome.config import Config
from socialhome.services.app_federation_service import AppFederationService


@pytest.fixture
async def cfg(tmp_dir):
    """Return a minimal standalone Config backed by tmp_dir."""
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
    )


async def test_create_app_returns_application(cfg):
    """create_app() returns an aiohttp.web.Application instance."""
    app = create_app(cfg)
    assert isinstance(app, web.Application)


async def test_create_app_has_routes(cfg):
    """create_app() registers at least the /healthz and /api/* routes."""
    app = create_app(cfg)
    resource_names = [r.canonical for r in app.router.resources()]
    assert "/healthz" in resource_names


async def test_create_app_stores_config(cfg):
    """create_app() stores the Config in the app dict under config_key."""
    from socialhome.app_keys import config_key

    app = create_app(cfg)
    assert app[config_key] is cfg


async def test_startup_hook_runs_without_error(tmp_dir):
    """Starting the app via TestClient triggers on_startup; identity auto-bootstraps."""
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        resp = await tc.get("/healthz")
        assert resp.status == 200
        # ensure_instance_identity ran — row exists, instance_id is in app dict.
        from socialhome.app_keys import instance_id_key

        assert app[instance_id_key] != "unknown"
        assert len(app[instance_id_key]) > 0


async def test_shared_http_session_lifecycle(tmp_dir):
    """A single aiohttp.ClientSession is created at startup and closed on cleanup."""
    from socialhome.app_keys import http_session_key

    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        await tc.get("/healthz")
        session = app[http_session_key]
        assert session is not None
        assert session.closed is False

    # After the TestClient context exits, cleanup hooks have run.
    assert session.closed is True


async def test_create_app_without_config_uses_env_defaults():
    """create_app(None) falls back to Config.from_env() — doesn't raise."""
    import os

    with tempfile.TemporaryDirectory() as d:
        os.environ["SH_DATA_DIR"] = d
        os.environ["SH_DB_PATH"] = str(Path(d) / "test.db")
        try:
            app = create_app()
            assert isinstance(app, web.Application)
        finally:
            os.environ.pop("SH_DATA_DIR", None)
            os.environ.pop("SH_DB_PATH", None)


async def test_app_federation_service_wired_into_app(tmp_dir):
    """AppFederationService is registered in the app dict and handlers are live.

    After startup:
    - ``app[app_federation_service_key]`` is an ``AppFederationService``.
    - The federation event registry has handlers for APP_SESSION and APP_MESSAGE
      (registered by ``federation_service.attach_apps(...)``).
    - The FederationTransport was constructed with a non-None app_inbound_handler
      (federation_service._app_inbound_handler bound method).
    """
    from socialhome.app_keys import (
        app_federation_service_key,
        federation_service_key,
        federation_transport_key,
    )
    from socialhome.domain.federation import FederationEventType

    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)):
        # 1. Service is registered under its key.
        app_fed = app[app_federation_service_key]
        assert isinstance(app_fed, AppFederationService)

        # 2. federation_service event registry has handlers for both event types.
        fed_svc = app[federation_service_key]
        registry = fed_svc._event_registry  # noqa: SLF001
        assert registry.handler_count(FederationEventType.APP_SESSION) >= 1, (
            "APP_SESSION handler not registered — attach_apps() not called"
        )
        assert registry.handler_count(FederationEventType.APP_MESSAGE) >= 1, (
            "APP_MESSAGE handler not registered — attach_apps() not called"
        )

        # 3. The transport was built with the binary inbound handler threaded in.
        fed_transport = app[federation_transport_key]
        assert fed_transport._app_inbound_handler is not None, (  # noqa: SLF001
            "FederationTransport._app_inbound_handler is None — "
            "app_inbound_handler= kwarg missing from FederationTransport()"
        )
