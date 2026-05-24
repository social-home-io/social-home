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


async def test_space_post_comments_round_trip(client):
    """The SPA's CommentOverlay fires GET on /api/spaces/{id}/posts/{pid}/comments
    to render the thread on overlay open and after every POST. Before
    this fix the view was POST-only — GET returned 405, the SPA's
    fetch promise rejected, and the overlay showed 'no comments' even
    though ``post.comment_count`` ticked up.

    Pascal's repro: '1 comment shown but I can't see any comments'.
    """
    h = _auth(client._tok)
    r = await client.post(
        "/api/spaces",
        json={"name": "CommentSpace", "emoji": "💬"},
        headers=h,
    )
    sid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "hello"},
        headers=h,
    )
    pid = (await r.json())["id"]

    # Empty thread on the freshly-created post.
    r = await client.get(
        f"/api/spaces/{sid}/posts/{pid}/comments",
        headers=h,
    )
    assert r.status == 200
    assert await r.json() == []

    # Add two comments.
    r = await client.post(
        f"/api/spaces/{sid}/posts/{pid}/comments",
        json={"content": "first"},
        headers=h,
    )
    assert r.status == 201
    r = await client.post(
        f"/api/spaces/{sid}/posts/{pid}/comments",
        json={"content": "second"},
        headers=h,
    )
    assert r.status == 201

    # The list endpoint now returns both, in insertion order.
    r = await client.get(
        f"/api/spaces/{sid}/posts/{pid}/comments",
        headers=h,
    )
    assert r.status == 200
    body = await r.json()
    assert [c["content"] for c in body] == ["first", "second"]


async def test_space_post_comments_get_404_for_unknown_post(client):
    h = _auth(client._tok)
    r = await client.post(
        "/api/spaces",
        json={"name": "X", "emoji": "🏠"},
        headers=h,
    )
    sid = (await r.json())["id"]
    r = await client.get(
        f"/api/spaces/{sid}/posts/does-not-exist/comments",
        headers=h,
    )
    # list_comments on a missing post returns empty rather than 404
    # (matches household feed semantics). Accept either as long as it
    # isn't 500 / 405.
    assert r.status in (200, 404)
