"""HTTP tests for /api/gfs/* routes."""

from __future__ import annotations

from socialhome.app_keys import gfs_connection_service_key
from socialhome.auth import sha256_token_hash
from socialhome.domain.federation import GfsConnection
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo

from .conftest import _auth


class _StubResp:
    def __init__(self, status: int, body: dict | None = None):
        self.status = status
        self._body = body or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body

    async def text(self):
        return ""


class _StubSession:
    """Minimal aiohttp-session stub for the publish round-trip."""

    def __init__(self, *, status: int = 200, body: dict | None = None):
        self._status = status
        self._body = body or {}

    def post(self, url, **kw):
        return _StubResp(self._status, self._body)

    def delete(self, url, **kw):
        return _StubResp(self._status, self._body)


def _stub_session(client, *, status: int = 200, body: dict | None = None) -> None:
    """Swap the wired GFS service's HTTP client for a stub so publish /
    unpublish round-trips don't hit the network."""
    svc = client.app[gfs_connection_service_key]
    svc._http_client = _StubSession(status=status, body=body)


def _make_conn(
    gfs_id: str = "gfs-1",
    *,
    status: str = "active",
    inbox_url: str = "https://gfs.example.com",
) -> GfsConnection:
    return GfsConnection(
        id=gfs_id,
        gfs_instance_id=f"inst-{gfs_id}",
        display_name=f"GFS {gfs_id}",
        public_key="pubkey-hex",
        inbox_url=inbox_url,
        status=status,
        paired_at="2025-01-01T00:00:00+00:00",
    )


async def _seed_gfs(client, gfs_id: str = "gfs-1", *, status: str = "active"):
    repo = SqliteGfsConnectionRepo(client._db)
    await repo.save(_make_conn(gfs_id, status=status))


# ─── GET /api/gfs/connections ────────────────────────────────────────


async def test_list_requires_auth(client):
    r = await client.get("/api/gfs/connections")
    assert r.status == 401


async def test_list_empty(client):
    r = await client.get("/api/gfs/connections", headers=_auth(client._tok))
    assert r.status == 200
    assert await r.json() == []


async def test_list_returns_connections_of_every_status(client):
    # The UI list must surface pending/suspended connections too — the
    # SPA distinguishes them by ``status``. Active-only made a freshly
    # connected (still-pending) GFS invisible.
    await _seed_gfs(client, "gfs-1")
    await _seed_gfs(client, "gfs-2", status="suspended")
    await _seed_gfs(client, "gfs-3", status="pending")
    r = await client.get("/api/gfs/connections", headers=_auth(client._tok))
    body = await r.json()
    assert {c["id"] for c in body} == {"gfs-1", "gfs-2", "gfs-3"}
    assert {c["status"] for c in body} == {"active", "suspended", "pending"}
    # public_key should be stripped from the response.
    assert all("public_key" not in c for c in body)


# ─── POST /api/gfs/connections (pair) ────────────────────────────────


async def test_pair_requires_admin(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('bob', 'bob-id', 'Bob', 0)",
    )
    raw = "bob-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES('tb', 'bob-id', 't', ?)",
        (sha256_token_hash(raw),),
    )
    r = await client.post(
        "/api/gfs/connections",
        json={"gfs_url": "https://x.com", "token": "t", "public_key": "pk"},
        headers=_auth(raw),
    )
    assert r.status == 403


async def test_pair_missing_fields_returns_422(client):
    r = await client.post(
        "/api/gfs/connections",
        json={"gfs_url": "https://x.com"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


# ─── GET /api/gfs/connections/{id} ──────────────────────────────────


async def test_detail_returns_connection(client):
    await _seed_gfs(client, "gfs-1")
    r = await client.get(
        "/api/gfs/connections/gfs-1",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["id"] == "gfs-1"
    assert "public_key" not in body


async def test_detail_not_found(client):
    r = await client.get(
        "/api/gfs/connections/nonexistent",
        headers=_auth(client._tok),
    )
    assert r.status == 404


# ─── DELETE /api/gfs/connections/{id} ────────────────────────────────


async def test_disconnect_requires_admin(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('bob2', 'bob2-id', 'Bob2', 0)",
    )
    raw = "bob2-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES('tb2', 'bob2-id', 't', ?)",
        (sha256_token_hash(raw),),
    )
    await _seed_gfs(client, "gfs-1")
    r = await client.delete(
        "/api/gfs/connections/gfs-1",
        headers=_auth(raw),
    )
    assert r.status == 403


async def test_disconnect_success(client):
    await _seed_gfs(client, "gfs-1")
    r = await client.delete(
        "/api/gfs/connections/gfs-1",
        headers=_auth(client._tok),
    )
    assert r.status == 204
    # Verify it's gone.
    r = await client.get(
        "/api/gfs/connections/gfs-1",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_disconnect_not_found(client):
    r = await client.delete(
        "/api/gfs/connections/nonexistent",
        headers=_auth(client._tok),
    )
    assert r.status == 404


# ─── POST /api/spaces/{id}/publish/{gfs_id} ─────────────────────────


async def test_publish_requires_admin(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('bob3', 'bob3-id', 'Bob3', 0)",
    )
    raw = "bob3-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES('tb3', 'bob3-id', 't', ?)",
        (sha256_token_hash(raw),),
    )
    r = await client.post(
        "/api/spaces/sp-1/publish/gfs-1",
        headers=_auth(raw),
    )
    assert r.status == 403


async def test_publish_gfs_not_found(client):
    r = await client.post(
        "/api/spaces/sp-1/publish/nonexistent",
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_publish_returns_publication_with_status(client):
    await _seed_gfs(client, "gfs-1")
    _stub_session(client, status=200, body={"status": "pending"})
    r = await client.post(
        "/api/spaces/sp-1/publish/gfs-1",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["space_id"] == "sp-1"
    assert body["gfs_connection_id"] == "gfs-1"
    assert body["status"] == "pending"
    assert "published_at" in body


async def test_publish_returns_422_when_gfs_rejects(client):
    await _seed_gfs(client, "gfs-1")
    _stub_session(client, status=500)
    r = await client.post(
        "/api/spaces/sp-1/publish/gfs-1",
        headers=_auth(client._tok),
    )
    assert r.status == 422


# ─── GET /api/spaces/{id}/publications ───────────────────────────────


async def test_space_publications_requires_auth(client):
    r = await client.get("/api/spaces/sp-1/publications")
    assert r.status == 401


async def test_space_publications_requires_admin(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('bob4', 'bob4-id', 'Bob4', 0)",
    )
    raw = "bob4-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES('tb4', 'bob4-id', 't', ?)",
        (sha256_token_hash(raw),),
    )
    r = await client.get(
        "/api/spaces/sp-1/publications",
        headers=_auth(raw),
    )
    assert r.status == 403


async def test_space_publications_returns_array(client):
    await _seed_gfs(client, "gfs-1")
    _stub_session(client, status=200, body={"status": "active"})
    # Publish so there's a row to list.
    pr = await client.post(
        "/api/spaces/sp-1/publish/gfs-1",
        headers=_auth(client._tok),
    )
    assert pr.status == 200
    r = await client.get(
        "/api/spaces/sp-1/publications",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["space_id"] == "sp-1"
    assert body[0]["gfs_connection_id"] == "gfs-1"
    assert body[0]["status"] == "active"


async def test_space_publications_empty_array(client):
    r = await client.get(
        "/api/spaces/sp-no-pubs/publications",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert await r.json() == []


# ─── DELETE /api/spaces/{id}/publish/{gfs_id} ────────────────────────


async def test_unpublish_gfs_not_found(client):
    r = await client.delete(
        "/api/spaces/sp-1/publish/nonexistent",
        headers=_auth(client._tok),
    )
    assert r.status == 422
