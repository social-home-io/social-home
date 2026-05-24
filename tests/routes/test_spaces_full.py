"""Full route coverage for space endpoints."""

from .conftest import _auth


async def test_space_full_lifecycle(client):
    """Create → get → update → members → feed → posts → dissolve."""
    h = _auth(client._tok)
    r = await client.post(
        "/api/spaces", json={"name": "TestSpace", "emoji": "🏠"}, headers=h
    )
    assert r.status == 201
    sid = (await r.json())["id"]

    r = await client.get(f"/api/spaces/{sid}", headers=h)
    assert r.status == 200
    assert (await r.json())["name"] == "TestSpace"

    r = await client.patch(f"/api/spaces/{sid}", json={"name": "Updated"}, headers=h)
    assert r.status == 200

    r = await client.get(f"/api/spaces/{sid}/members", headers=h)
    assert r.status == 200
    assert len(await r.json()) >= 1

    r = await client.get(f"/api/spaces/{sid}/feed", headers=h)
    assert r.status == 200

    r = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "hello space"},
        headers=h,
    )
    assert r.status == 201

    r = await client.post(
        f"/api/spaces/{sid}/invite-tokens", json={"uses": 1}, headers=h
    )
    assert r.status == 201

    r = await client.post(
        f"/api/spaces/{sid}/ban", json={"user_id": "nonexistent"}, headers=h
    )
    # May return 404 if user doesn't exist or 200 — just ensure no 500
    assert r.status < 500

    r = await client.delete(f"/api/spaces/{sid}", headers=h)
    assert r.status == 200


async def test_space_post_delete_route_exists(client):
    """Regression — the SPA fires ``DELETE /api/spaces/{id}/posts/{post_id}``
    and aiohttp's routing layer must have a handler. Before this fix
    that path returned 404 at routing (no server log, SPA's async-fn
    rejection silently swallowed → confirmation dialog closed and
    the post stayed visible)."""
    h = _auth(client._tok)
    r = await client.post(
        "/api/spaces",
        json={"name": "PostDeleteSpace", "emoji": "🗑"},
        headers=h,
    )
    assert r.status == 201
    sid = (await r.json())["id"]

    r = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "delete me"},
        headers=h,
    )
    assert r.status == 201
    pid = (await r.json())["id"]

    r = await client.delete(f"/api/spaces/{sid}/posts/{pid}", headers=h)
    assert r.status == 204

    # Idempotency: deleting an already-deleted post is still 404 (post
    # gone from the author's view) — not 500.
    r = await client.delete(f"/api/spaces/{sid}/posts/{pid}", headers=h)
    assert r.status in (204, 404)


async def test_space_post_delete_404_for_unknown_post(client):
    h = _auth(client._tok)
    r = await client.post(
        "/api/spaces",
        json={"name": "X", "emoji": "🏠"},
        headers=h,
    )
    sid = (await r.json())["id"]
    r = await client.delete(
        f"/api/spaces/{sid}/posts/does-not-exist",
        headers=h,
    )
    assert r.status == 404


async def test_space_post_patch_edits_content(client):
    h = _auth(client._tok)
    r = await client.post(
        "/api/spaces",
        json={"name": "EditSpace", "emoji": "✏"},
        headers=h,
    )
    sid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "original"},
        headers=h,
    )
    pid = (await r.json())["id"]
    r = await client.patch(
        f"/api/spaces/{sid}/posts/{pid}",
        json={"content": "edited"},
        headers=h,
    )
    assert r.status == 200
    body = await r.json()
    assert body["content"] == "edited"
    assert body["edited_at"] is not None
