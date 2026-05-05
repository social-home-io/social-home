"""HTTP tests for /api/moments/* (§Momentum)."""

from __future__ import annotations

from .conftest import _auth


async def _seed_target(client, *, user_id="uid-bob", username="bob"):
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        (username, user_id, "Bob"),
    )


# ── Create + list ────────────────────────────────────────────────────────


async def test_create_text_moment_and_list(client):
    r = await client.post(
        "/api/moments",
        json={"content": "hello world"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["content"] == "hello world"
    assert body["author_user_id"] == client._uid
    assert body["origin_instance_id"]

    r = await client.get("/api/moments", headers=_auth(client._tok))
    assert r.status == 200
    items = await r.json()
    assert {m["id"] for m in items} == {body["id"]}


async def test_create_empty_rejected(client):
    r = await client.post(
        "/api/moments",
        json={"content": "  "},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_rate_limit_429_within_window(client):
    r = await client.post(
        "/api/moments",
        json={"content": "one"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    r = await client.post(
        "/api/moments",
        json={"content": "two"},
        headers=_auth(client._tok),
    )
    assert r.status == 429
    body = await r.json()
    assert body["error"]["code"] == "MOMENT_RATE_LIMIT"


async def test_video_over_15s_rejected(client):
    r = await client.post(
        "/api/moments",
        json={
            "content": "",
            "media_url": "/api/media/clip.webm",
            "media_type": "video",
            "duration_ms": 16_000,
        },
        headers=_auth(client._tok),
    )
    assert r.status == 422


# ── Replies ──────────────────────────────────────────────────────────────


async def test_reply_skips_rate_limit(client):
    r = await client.post(
        "/api/moments",
        json={"content": "root"},
        headers=_auth(client._tok),
    )
    parent_id = (await r.json())["id"]
    # Replying again is fine even within the 15-min window.
    r = await client.post(
        "/api/moments",
        json={"content": "self-reply", "parent_moment_id": parent_id},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    assert (await r.json())["parent_moment_id"] == parent_id


async def test_detail_includes_replies_and_reactions(client):
    r = await client.post(
        "/api/moments",
        json={"content": "root"},
        headers=_auth(client._tok),
    )
    parent_id = (await r.json())["id"]
    await client.post(
        "/api/moments",
        json={"content": "r", "parent_moment_id": parent_id},
        headers=_auth(client._tok),
    )
    await client.put(
        f"/api/moments/{parent_id}/reaction",
        json={"emoji": "🔥"},
        headers=_auth(client._tok),
    )
    r = await client.get(
        f"/api/moments/{parent_id}",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["moment"]["id"] == parent_id
    assert len(body["replies"]) == 1
    assert [rx["emoji"] for rx in body["reactions"]] == ["🔥"]


# ── Block visibility ─────────────────────────────────────────────────────


async def test_blocked_authors_hidden_from_inbox(client):
    """Someone the viewer blocked drops out of the moments inbox."""
    await _seed_target(client)
    # Bob posts a moment via his own user_id (we hit the repo directly
    # because there's no other auth context wired in this test).
    await client._db.enqueue(
        "INSERT INTO moments(id, author_user_id, content, "
        "origin_instance_id, created_at, expires_at) "
        "VALUES(?,?,?,?, datetime('now'), datetime('now', '+7 day'))",
        ("m-bob", "uid-bob", "from bob", "self"),
    )
    r = await client.get("/api/moments", headers=_auth(client._tok))
    assert any(m["author_user_id"] == "uid-bob" for m in await r.json())
    # Block bob → his moment disappears.
    r = await client.post(
        "/api/blocks",
        json={"user_id": "uid-bob"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    r = await client.get("/api/moments", headers=_auth(client._tok))
    assert not any(m["author_user_id"] == "uid-bob" for m in await r.json())


# ── Report ───────────────────────────────────────────────────────────────


async def test_report_moment_creates_pending_row(client):
    r = await client.post(
        "/api/moments",
        json={"content": "x"},
        headers=_auth(client._tok),
    )
    moment_id = (await r.json())["id"]
    # The reporter must not be the same as the report's only filer (no
    # self-report duplicates), so seed a second reporter via a fresh user.
    await _seed_target(client, user_id="uid-bob", username="bob")
    r = await client.post(
        f"/api/moments/{moment_id}/report",
        json={"category": "spam", "notes": "promotion"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["status"] == "pending"


# ── Follows ──────────────────────────────────────────────────────────────


async def test_follow_unfollow_round_trip(client):
    await _seed_target(client)
    r = await client.post(
        "/api/moments/follows",
        json={"user_id": "uid-bob"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    r = await client.get("/api/moments/follows", headers=_auth(client._tok))
    body = await r.json()
    assert [f["user_id"] for f in body["follows"]] == ["uid-bob"]
    r = await client.delete(
        "/api/moments/follows/uid-bob",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await (
        await client.get(
            "/api/moments/follows",
            headers=_auth(client._tok),
        )
    ).json()
    assert body["follows"] == []


async def test_follow_self_rejected(client):
    r = await client.post(
        "/api/moments/follows",
        json={"user_id": client._uid},
        headers=_auth(client._tok),
    )
    assert r.status == 422
