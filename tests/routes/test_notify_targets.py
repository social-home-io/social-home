"""HTTP tests for ``GET /api/me/notify-targets``."""

from __future__ import annotations

from socialhome.app_keys import platform_adapter_key

from .conftest import _auth


async def test_get_requires_auth(client):
    r = await client.get("/api/me/notify-targets")
    assert r.status == 401


async def test_returns_empty_list_in_standalone(client):
    r = await client.get("/api/me/notify-targets", headers=_auth(client._tok))
    assert r.status == 200
    assert (await r.json()) == {"targets": []}


async def test_surfaces_targets_from_push_provider(client):
    class _StubPush:
        async def list_notify_targets(self):
            return [{"entity_id": "notify.mobile_app_x", "name": "Mobile App X"}]

    client.app[platform_adapter_key].push = _StubPush()
    r = await client.get("/api/me/notify-targets", headers=_auth(client._tok))
    assert r.status == 200
    assert (await r.json())["targets"] == [
        {"entity_id": "notify.mobile_app_x", "name": "Mobile App X"}
    ]


async def test_empty_when_push_is_none(client):
    client.app[platform_adapter_key].push = None
    r = await client.get("/api/me/notify-targets", headers=_auth(client._tok))
    assert r.status == 200
    assert (await r.json()) == {"targets": []}
