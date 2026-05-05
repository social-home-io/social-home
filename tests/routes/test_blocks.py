"""HTTP tests for /api/blocks/* — personal user blocks (§Privacy)."""

from __future__ import annotations

from .conftest import _auth


async def _seed_target(client, *, user_id="uid-bob", username="bob"):
    """Insert a second local user that admin can block."""
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        (username, user_id, "Bob"),
    )


async def test_get_blocks_empty(client):
    r = await client.get("/api/blocks", headers=_auth(client._tok))
    assert r.status == 200
    assert (await r.json()) == {"blocks": []}


async def test_post_block_then_get(client):
    await _seed_target(client)
    r = await client.post(
        "/api/blocks",
        json={"user_id": "uid-bob"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    assert (await r.json()) == {"user_id": "uid-bob"}

    r = await client.get("/api/blocks", headers=_auth(client._tok))
    body = await r.json()
    assert [b["user_id"] for b in body["blocks"]] == ["uid-bob"]
    assert body["blocks"][0]["blocked_at"]


async def test_post_block_self_rejected(client):
    """Self-block raises ValueError → maps to 422 via BaseView._iter."""
    r = await client.post(
        "/api/blocks",
        json={"user_id": client._uid},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_post_block_missing_user_id(client):
    r = await client.post("/api/blocks", json={}, headers=_auth(client._tok))
    assert r.status == 400


async def test_delete_block(client):
    await _seed_target(client)
    await client.post(
        "/api/blocks",
        json={"user_id": "uid-bob"},
        headers=_auth(client._tok),
    )
    r = await client.delete(
        "/api/blocks/uid-bob",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    r = await client.get("/api/blocks", headers=_auth(client._tok))
    assert (await r.json()) == {"blocks": []}


async def test_delete_block_idempotent(client):
    """Deleting a non-existent block is a no-op (200)."""
    r = await client.delete(
        "/api/blocks/uid-ghost",
        headers=_auth(client._tok),
    )
    assert r.status == 200


async def test_blocks_require_auth(client):
    r = await client.get("/api/blocks")
    assert r.status == 401
    r = await client.post("/api/blocks", json={"user_id": "uid-bob"})
    assert r.status == 401
    r = await client.delete("/api/blocks/uid-bob")
    assert r.status == 401
