"""Tests for the GFS public story routes (§stories_public).

The signed wire endpoints (publish / mint / revoke / unpublish) require
Ed25519-authenticated bodies. Rather than rebuild the full signing
flow here we exercise the public landing page end-to-end and unit-test
the registry directly in ``test_story_publications`` for the rest.
The signed handlers themselves get a smoke test that returns 401 on a
missing signature so the auth wiring is at least live.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.global_server.app_keys import (
    gfs_fed_repo_key,
    gfs_story_pub_service_key,
    gfs_ws_registry_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.server import create_gfs_app


def _config(tmp_dir):
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id="gfs-test",
    )


@pytest.fixture
async def client(tmp_dir):
    app = create_gfs_app(_config(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        tc._app = app
        # Seed the author instance the publication points at — the FK
        # on gfs_story_publications.instance_id would otherwise reject
        # the upsert.
        await app[gfs_fed_repo_key].upsert_instance(
            ClientInstance(
                instance_id="inst-author",
                display_name="Author",
                public_key="aa" * 32,
                inbox_url="http://author/wh",
                status="active",
            )
        )
        yield tc


def _mark_author_online(app, instance_id: str = "inst-author") -> None:
    """Stub the WS registry as if the author were connected."""

    class _StubWs:
        closed = False

    app[gfs_ws_registry_key]._by_instance[instance_id] = _StubWs()


# ── Public landing page ─────────────────────────────────────────────────


async def test_landing_returns_200_when_token_active_and_author_online(client):
    app = client._app
    registry = app[gfs_story_pub_service_key]
    tok, url = await registry.record_publish(
        story_id="s-1",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        publish_signature="",
    )
    _mark_author_online(app)
    # ``url`` points at the configured base_url; we hit the local
    # path-only counterpart against the test client.
    path = f"/story/inst-author/s-1/{tok.token}"
    assert path in url

    resp = await client.get(path)
    assert resp.status == 200
    text = await resp.text()
    assert "Story coming soon" in text or "<html" in text


async def test_landing_returns_410_when_token_revoked(client):
    app = client._app
    registry = app[gfs_story_pub_service_key]
    tok, _url = await registry.record_publish(
        story_id="s-1",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        publish_signature="",
    )
    await registry.revoke_token(tok.token, "inst-author")
    _mark_author_online(app)
    resp = await client.get(f"/story/inst-author/s-1/{tok.token}")
    assert resp.status == 410


async def test_landing_returns_410_when_unpublished(client):
    app = client._app
    registry = app[gfs_story_pub_service_key]
    tok, _url = await registry.record_publish(
        story_id="s-1",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        publish_signature="",
    )
    await registry.remove_publish("s-1", "inst-author")
    resp = await client.get(f"/story/inst-author/s-1/{tok.token}")
    assert resp.status == 410


async def test_landing_returns_410_when_url_mixes_wrong_story_id(client):
    app = client._app
    registry = app[gfs_story_pub_service_key]
    tok, _url = await registry.record_publish(
        story_id="s-1",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        publish_signature="",
    )
    _mark_author_online(app)
    # Same token, different story_id in the path — must not resolve.
    resp = await client.get(f"/story/inst-author/s-OTHER/{tok.token}")
    assert resp.status == 410


async def test_landing_returns_503_when_author_offline(client):
    app = client._app
    registry = app[gfs_story_pub_service_key]
    tok, _url = await registry.record_publish(
        story_id="s-1",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        publish_signature="",
    )
    # Don't mark author online.
    resp = await client.get(f"/story/inst-author/s-1/{tok.token}")
    assert resp.status == 503


async def test_landing_returns_410_for_unknown_token(client):
    resp = await client.get("/story/inst-author/s-1/never-issued")
    assert resp.status == 410


# ── Signed-wire smoke ────────────────────────────────────────────────────


async def test_publish_endpoint_rejects_missing_signature(client):
    """No signature on the body → 401 from `_rtc_authenticate`."""
    resp = await client.post(
        "/gfs/stories/s-1/publish",
        json={"instance_id": "inst-author", "story_id": "s-1", "expires_at": 1},
    )
    assert resp.status in (401, 403)
