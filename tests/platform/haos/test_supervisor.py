"""Tests for socialhome.platform.haos.supervisor."""

from __future__ import annotations

import pytest
from aiohttp import web

from socialhome.platform.haos.supervisor import SupervisorClient


@pytest.fixture
async def sv_server(aiohttp_server):
    captured: dict = {
        "discovery": [],
        "auth_response": None,
        "validate_status": 200,
        "validate_calls": [],
    }

    async def auth_list(request: web.Request) -> web.Response:
        captured["auth_header"] = request.headers.get("Authorization")
        return web.json_response(
            captured["auth_response"]
            or {
                "data": {
                    "users": [
                        {
                            "username": "ha_owner",
                            "is_owner": True,
                            "system_generated": False,
                        },
                        {
                            "username": "system",
                            "is_owner": False,
                            "system_generated": True,
                        },
                    ]
                }
            },
        )

    async def discovery(request: web.Request) -> web.Response:
        body = await request.json()
        captured["discovery"].append(body)
        return web.json_response({"result": "ok"})

    async def validate_session(request: web.Request) -> web.Response:
        body = await request.json()
        captured["validate_calls"].append(body)
        return web.json_response({}, status=captured["validate_status"])

    app = web.Application()
    app.router.add_get("/auth/list", auth_list)
    app.router.add_post("/discovery", discovery)
    # Real Supervisor route (see ``supervisor/api/__init__.py`` upstream
    # — ``web.post("/ingress/validate_session", api_ingress.validate_session)``).
    # The earlier code in this repo posted to the wrong path
    # (``/ingress/session/validate``) which 404'd on every call.
    app.router.add_post("/ingress/validate_session", validate_session)
    server = await aiohttp_server(app)
    return server, captured


@pytest.fixture
async def client(sv_server):
    import aiohttp

    server, _ = sv_server
    async with aiohttp.ClientSession() as session:
        yield SupervisorClient(
            session, str(server.make_url("")).rstrip("/"), "sv-token"
        )


async def test_get_owner_username_returns_non_system_owner(client, sv_server):
    _, captured = sv_server
    owner = await client.get_owner_username()
    assert owner == "ha_owner"
    assert captured["auth_header"] == "Bearer sv-token"


async def test_get_owner_username_returns_none_when_no_owner(client, sv_server):
    _, captured = sv_server
    captured["auth_response"] = {
        "data": {
            "users": [
                {"username": "system", "is_owner": False, "system_generated": True},
            ]
        }
    }
    assert await client.get_owner_username() is None


async def test_push_discovery_true_on_2xx(client, sv_server):
    _, captured = sv_server
    ok = await client.push_discovery(
        {"service": "socialhome", "config": {"token": "abc"}},
    )
    assert ok is True
    assert captured["discovery"] == [
        {"service": "socialhome", "config": {"token": "abc"}},
    ]


async def test_validate_session_token_hits_correct_path(client, sv_server):
    _, captured = sv_server
    captured["validate_status"] = 200
    assert await client.validate_session_token("session-x") is True
    assert captured["validate_calls"] == [{"session": "session-x"}]


async def test_validate_session_token_rejects_on_4xx(client, sv_server):
    _, captured = sv_server
    captured["validate_status"] = 401
    assert await client.validate_session_token("bad") is False


async def test_validate_session_token_rejects_empty(client):
    """No round-trip; empty token short-circuits to False."""
    assert await client.validate_session_token("") is False
