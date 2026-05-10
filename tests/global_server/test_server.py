"""Tests for create_gfs_app() — GFS application factory."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from socialhome.global_server import create_gfs_app


@pytest.fixture
async def gfs_client(tmp_path):
    """A running GFS app client backed by a temp SQLite database."""
    app = create_gfs_app(db_path=tmp_path / "gfs.db")
    async with TestClient(TestServer(app)) as tc:
        yield tc


async def _fresh_pair_token(app, client_ip: str = "127.0.0.99") -> str:
    """Mint a one-time pair token via the in-process token service.

    The landing page issues these on a per-IP rate limit; in tests we
    bypass the rate limiter by passing a different IP each call.
    """
    token_svc = app["gfs_token_service"]
    token, _wait = await token_svc.generate(client_ip)
    assert token is not None, "token service rate-limited the test"
    return token


async def test_create_gfs_app_returns_application(tmp_path):
    """create_gfs_app() returns an aiohttp.web.Application instance."""
    app = create_gfs_app(db_path=tmp_path / "gfs_check.db")
    assert isinstance(app, web.Application)


def _route_paths(app):
    """Return the set of canonical route paths registered on *app*.

    With the ``BaseView`` subclass refactor (Session 16e), routes are
    registered via ``app.router.add_view`` which uses the ``*`` method
    and dispatches internally; checking paths alone is sufficient for
    smoke tests.
    """
    return {r.resource.canonical for r in app.router.routes()}


async def test_gfs_app_has_register_route(tmp_path):
    """The GFS app exposes /gfs/register."""
    app = create_gfs_app(db_path=tmp_path / "gfs.db")
    assert "/gfs/register" in _route_paths(app)


async def test_gfs_app_has_publish_route(tmp_path):
    """The GFS app exposes /gfs/publish."""
    app = create_gfs_app(db_path=tmp_path / "gfs.db")
    assert "/gfs/publish" in _route_paths(app)


async def test_gfs_app_has_subscribe_route(tmp_path):
    """The GFS app exposes /gfs/subscribe."""
    app = create_gfs_app(db_path=tmp_path / "gfs.db")
    assert "/gfs/subscribe" in _route_paths(app)


async def test_gfs_app_has_spaces_route(tmp_path):
    """The GFS app exposes /gfs/spaces."""
    app = create_gfs_app(db_path=tmp_path / "gfs.db")
    assert "/gfs/spaces" in _route_paths(app)


async def test_gfs_app_has_healthz_route(tmp_path):
    """The GFS app exposes /healthz."""
    app = create_gfs_app(db_path=tmp_path / "gfs.db")
    assert "/healthz" in _route_paths(app)


async def test_healthz_returns_200(gfs_client):
    """GET /healthz returns HTTP 200."""
    resp = await gfs_client.get("/healthz")
    assert resp.status == 200


async def test_healthz_returns_ok_body(gfs_client):
    """GET /healthz returns JSON body {"status": "ok"}."""
    resp = await gfs_client.get("/healthz")
    body = await resp.json()
    assert body == {"status": "ok"}


async def test_gfs_spaces_returns_empty_list_initially(gfs_client):
    """GET /gfs/spaces returns an empty list when no spaces have been published."""
    resp = await gfs_client.get("/gfs/spaces")
    assert resp.status == 200
    body = await resp.json()
    assert body == {"spaces": []}


async def test_register_instance_returns_registered(gfs_client):
    """POST /gfs/register returns {"status": "registered"} for a valid payload."""
    token = await _fresh_pair_token(gfs_client.server.app, "127.0.0.10")
    resp = await gfs_client.post(
        "/gfs/register",
        json={
            "token": token,
            "instance_id": "inst-abc",
            "public_key": "aa" * 32,
            "inbox_url": "http://example.com/inbox",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "registered"
    assert body["instance_id"] == "inst-abc"


async def test_register_missing_field_returns_400(gfs_client):
    """POST /gfs/register with missing fields returns HTTP 400."""
    token = await _fresh_pair_token(gfs_client.server.app, "127.0.0.11")
    resp = await gfs_client.post(
        "/gfs/register",
        json={"token": token, "instance_id": "inst-abc"},
    )
    assert resp.status == 400


async def test_register_missing_token_returns_400(gfs_client):
    """POST /gfs/register without a token must be rejected — accepting
    anonymous registrations would let anyone show up at the GFS."""
    resp = await gfs_client.post(
        "/gfs/register",
        json={
            "instance_id": "inst-no-tok",
            "public_key": "aa" * 32,
            "inbox_url": "http://example.com/inbox",
        },
    )
    assert resp.status == 400


async def test_register_invalid_token_returns_401(gfs_client):
    resp = await gfs_client.post(
        "/gfs/register",
        json={
            "token": "this-was-never-minted",
            "instance_id": "inst-bad-tok",
            "public_key": "aa" * 32,
            "inbox_url": "http://example.com/inbox",
        },
    )
    assert resp.status == 401
    body = await resp.json()
    assert body["error"] == "invalid_or_expired_token"


async def test_register_token_is_single_use(gfs_client):
    """A token consumed once cannot be replayed."""
    token = await _fresh_pair_token(gfs_client.server.app, "127.0.0.12")
    body = {
        "token": token,
        "instance_id": "inst-replay",
        "public_key": "aa" * 32,
        "inbox_url": "http://example.com/inbox",
    }
    first = await gfs_client.post("/gfs/register", json=body)
    assert first.status == 200
    second = await gfs_client.post("/gfs/register", json=body)
    assert second.status == 401


async def test_register_returns_pending_when_auto_accept_off(gfs_client):
    """When policy has auto_accept_clients=0 the register response is 'pending'."""
    from socialhome.global_server.app_keys import gfs_admin_repo_key

    app = gfs_client.server.app
    await app[gfs_admin_repo_key].set_config("auto_accept_clients", "0")
    token = await _fresh_pair_token(app, "127.0.0.13")
    resp = await gfs_client.post(
        "/gfs/register",
        json={
            "token": token,
            "instance_id": "new-pending.home",
            "public_key": "aa" * 32,
            "inbox_url": "http://p/wh",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "pending"


async def test_gfs_info_returns_public_key(gfs_client):
    """``GET /gfs/info`` exposes the GFS's Ed25519 public key so HFS
    clients can pin it after scanning the QR (which carries only
    ``base_url`` + ``token``).
    """
    resp = await gfs_client.get("/gfs/info")
    assert resp.status == 200
    body = await resp.json()
    assert body["gfs_instance_id"]
    assert body["public_key"]
    assert len(body["public_key"]) == 64  # Ed25519 hex
    assert body["server_name"]


async def test_admin_static_index_served(gfs_client):
    """GET /admin returns the single-page HTML dashboard."""
    resp = await gfs_client.get("/admin")
    assert resp.status == 200
    text = await resp.text()
    assert "<!doctype" in text.lower() or "<html" in text.lower()
    assert "GFS Admin" in text


async def test_healthz_is_public(gfs_client):
    """The admin auth middleware does not gate public endpoints."""
    resp = await gfs_client.get("/healthz")
    assert resp.status == 200


async def test_subscribe_returns_subscribed(gfs_client):
    """POST /gfs/subscribe returns {"status": "subscribed"}."""
    # Register first so the instance exists.
    token = await _fresh_pair_token(gfs_client.server.app, "127.0.0.20")
    await gfs_client.post(
        "/gfs/register",
        json={
            "token": token,
            "instance_id": "inst-sub",
            "public_key": "bb" * 32,
            "inbox_url": "http://example.com/wh",
        },
    )
    resp = await gfs_client.post(
        "/gfs/subscribe",
        json={"instance_id": "inst-sub", "space_id": "space-1"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "subscribed"


async def test_subscribe_unsubscribe_roundtrip(gfs_client):
    """POST /gfs/subscribe then unsubscribe returns correct statuses."""
    token = await _fresh_pair_token(gfs_client.server.app, "127.0.0.21")
    await gfs_client.post(
        "/gfs/register",
        json={
            "token": token,
            "instance_id": "inst-unsub",
            "public_key": "cc" * 32,
            "inbox_url": "http://example.com/wh2",
        },
    )
    await gfs_client.post(
        "/gfs/subscribe",
        json={"instance_id": "inst-unsub", "space_id": "space-X"},
    )
    resp = await gfs_client.post(
        "/gfs/subscribe",
        json={
            "instance_id": "inst-unsub",
            "space_id": "space-X",
            "action": "unsubscribe",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "unsubscribed"
