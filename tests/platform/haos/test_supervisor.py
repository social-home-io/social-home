"""Tests for socialhome.platform.haos.supervisor."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohasupervisor import SupervisorConnectionError, SupervisorError
from aiohasupervisor.models.discovery import DiscoveryConfig
from aiohttp import web

from socialhome.platform.haos.supervisor import SupervisorClient

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


# ── /auth/list — still goes through raw aiohttp (the library does not
# yet expose an auth client), so this branch keeps a real aiohttp test
# server. Discovery + ``/addons/self/info`` use ``aiohasupervisor`` and
# are mocked at the library boundary further down.


@pytest.fixture
async def auth_server(aiohttp_server):
    captured: dict = {"auth_response": None}

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

    app = web.Application()
    app.router.add_get("/auth/list", auth_list)
    server = await aiohttp_server(app)
    return server, captured


@pytest.fixture
async def auth_client(auth_server):
    server, _ = auth_server
    async with aiohttp.ClientSession() as session:
        yield SupervisorClient(
            session, str(server.make_url("")).rstrip("/"), "sv-token"
        )


async def test_get_owner_username_returns_non_system_owner(auth_client, auth_server):
    _, captured = auth_server
    owner = await auth_client.get_owner_username()
    assert owner == "ha_owner"
    assert captured["auth_header"] == "Bearer sv-token"


async def test_get_owner_username_returns_none_when_no_owner(auth_client, auth_server):
    _, captured = auth_server
    captured["auth_response"] = {
        "data": {
            "users": [
                {"username": "system", "is_owner": False, "system_generated": True},
            ]
        }
    }
    assert await auth_client.get_owner_username() is None


# ── Discovery + addon info — exercised through aiohasupervisor. We
# don't re-test the library's HTTP layer here (its own test suite
# does that, and ``InstalledAddonComplete`` has 40+ required fields
# which would make the fixture impossibly brittle); instead we patch
# at the library boundary and verify our wrapper's mapping logic.


@pytest.fixture
async def mocked_client():
    """``SupervisorClient`` with its ``_aha`` swapped for a MagicMock."""
    session = aiohttp.ClientSession()
    try:
        client = SupervisorClient(session, "http://supervisor", "sv-token")
        client._aha = MagicMock()
        client._aha.discovery = MagicMock()
        client._aha.discovery.set = AsyncMock()
        client._aha.addons = MagicMock()
        client._aha.addons.addon_info = AsyncMock()
        yield client
    finally:
        await session.close()


async def test_push_discovery_passes_service_and_config(mocked_client):
    ok = await mocked_client.push_discovery(
        "socialhome", {"host": "h", "port": 8099, "token": "abc"}
    )
    assert ok is True
    mocked_client._aha.discovery.set.assert_awaited_once_with(
        DiscoveryConfig(
            service="socialhome",
            config={"host": "h", "port": 8099, "token": "abc"},
        )
    )


async def test_push_discovery_returns_false_on_supervisor_error(mocked_client):
    mocked_client._aha.discovery.set.side_effect = SupervisorConnectionError("boom")
    assert (
        await mocked_client.push_discovery("socialhome", {"token": "abc"}) is False
    )


async def test_get_self_info_returns_typed_model(mocked_client):
    sentinel = MagicMock(hostname="local-social-home", ingress_port=8099)
    mocked_client._aha.addons.addon_info.return_value = sentinel
    info = await mocked_client.get_self_info()
    assert info is sentinel
    mocked_client._aha.addons.addon_info.assert_awaited_once_with("self")


async def test_get_self_info_returns_none_on_supervisor_error(mocked_client):
    mocked_client._aha.addons.addon_info.side_effect = SupervisorError("nope")
    assert await mocked_client.get_self_info() is None
