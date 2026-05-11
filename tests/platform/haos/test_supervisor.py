"""Tests for socialhome.platform.haos.supervisor."""

from __future__ import annotations

import pytest
from aiohttp import web

from socialhome.platform.haos.supervisor import AddonInfo, SupervisorClient

# pytest-homeassistant-custom-component (a transitive dev dep when this
# repo's venv is shared with the ha-integration repo) installs a
# socket-blocking guard; the aiohttp TestClient below needs a real
# port. CI doesn't install that plugin so this is a no-op there.
try:
    import pytest_socket  # noqa: F401

    @pytest.fixture(autouse=True)
    def _enable_sockets(socket_enabled):
        """Re-enable sockets if the HA pytest plugin disabled them."""

except ImportError:  # pragma: no cover - CI path
    pass


@pytest.fixture
async def sv_server(aiohttp_server):
    captured: dict = {"discovery": [], "auth_response": None}

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

    async def self_info(request: web.Request) -> web.Response:
        captured["self_info_header"] = request.headers.get("Authorization")
        return web.json_response(
            captured.get("self_info_response")
            or {
                "result": "ok",
                "data": {
                    "slug": "local_social_home",
                    "hostname": "local-social-home",
                    "ingress_port": 8099,
                    "ip_address": "172.30.33.0",
                },
            }
        )

    app = web.Application()
    app.router.add_get("/auth/list", auth_list)
    app.router.add_post("/discovery", discovery)
    app.router.add_get("/addons/self/info", self_info)
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


async def test_get_self_info_returns_typed_addon_info(client, sv_server):
    _, captured = sv_server
    info = await client.get_self_info()
    assert info == AddonInfo(hostname="local-social-home", ingress_port=8099)
    assert captured["self_info_header"] == "Bearer sv-token"


async def test_get_self_info_returns_none_when_response_missing_fields(
    client, sv_server
):
    """Partial responses (missing ``hostname`` or ``ingress_port``) surface as
    ``None`` rather than as a half-populated dataclass — the bootstrap
    treats ``None`` as "skip discovery this boot"."""
    _, captured = sv_server
    captured["self_info_response"] = {
        "result": "ok",
        "data": {"slug": "local_social_home", "hostname": "local-social-home"},
    }
    assert await client.get_self_info() is None


async def test_get_self_info_returns_none_when_supervisor_unreachable():
    """A bad URL surfaces as ``None`` (no exception escapes)."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        # Port 1 is reserved and refused on every sane host; the
        # client wraps the connection error into a warning + ``None``.
        sv = SupervisorClient(session, "http://127.0.0.1:1", "sv-token")
        assert await sv.get_self_info() is None


def test_addon_info_from_response_rejects_partial_payloads():
    """The parser returns ``None`` for any missing required field."""
    assert AddonInfo.from_response(None) is None
    assert AddonInfo.from_response({}) is None
    assert AddonInfo.from_response({"hostname": "h"}) is None
    assert AddonInfo.from_response({"ingress_port": 8099}) is None
    # Both present -> parses.
    info = AddonInfo.from_response({"hostname": "h", "ingress_port": "8099"})
    assert info == AddonInfo(hostname="h", ingress_port=8099)
