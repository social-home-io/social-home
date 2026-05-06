"""HTTP smoke tests for the highlights routes."""

from __future__ import annotations


def _auth(client) -> dict:
    return {"Authorization": f"Bearer {client._tok}"}


async def test_create_frame_then_list(client):
    """POST /api/highlights/frames creates a highlight; GET /api/highlights returns it."""
    resp = await client.post(
        "/api/highlights/frames",
        json={
            "frame_type": "image",
            "media_url": "/api/media/x.webp",
            "caption_text": "hello",
            "caption_emoji": "🌅",
        },
        headers=_auth(client),
    )
    assert resp.status == 201, await resp.text()
    body = await resp.json()
    assert body["highlight"]["author_user_id"] == client._uid
    assert body["frame"]["sequence"] == 1
    assert body["frame"]["caption_emoji"] == "🌅"

    listed = await client.get("/api/highlights", headers=_auth(client))
    assert listed.status == 200
    rows = await listed.json()
    assert len(rows) == 1
    assert len(rows[0]["frames"]) == 1


async def test_react_and_clear(client):
    """PUT /reaction sets, DELETE /reaction clears."""
    resp = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    assert resp.status == 201
    body = await resp.json()
    frame_id = body["frame"]["id"]
    # Authors don't accumulate views on their own highlights, but reactions
    # are still recorded — we just want to exercise the endpoints.
    r = await client.put(
        f"/api/highlights/frames/{frame_id}/reaction",
        json={"emoji": "🔥"},
        headers=_auth(client),
    )
    assert r.status == 200
    r = await client.delete(
        f"/api/highlights/frames/{frame_id}/reaction",
        headers=_auth(client),
    )
    assert r.status == 200


async def test_share_to_household_feed(client):
    """POST /api/highlights/{id}/share creates a highlight_share post."""
    resp = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    body = await resp.json()
    highlight_id = body["highlight"]["id"]
    s = await client.post(
        f"/api/highlights/{highlight_id}/share",
        json={"scope": "household", "note": "look at this"},
        headers=_auth(client),
    )
    assert s.status == 201, await s.text()
    j = await s.json()
    assert j["highlight_id"] == highlight_id
    assert j["post_id"]


async def test_delete_highlight(client):
    resp = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    highlight_id = (await resp.json())["highlight"]["id"]
    d = await client.delete(f"/api/highlights/{highlight_id}", headers=_auth(client))
    assert d.status == 204
    listed = await client.get("/api/highlights", headers=_auth(client))
    rows = await listed.json()
    assert rows == []


async def test_create_frame_validates_inputs(client):
    """Bad bodies surface 400; bad enum values surface 400 too."""
    # Missing media_url
    r = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image"},
        headers=_auth(client),
    )
    assert r.status == 400

    # Invalid frame_type
    r = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "audio", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    assert r.status == 400

    # Invalid audience_kind
    r = await client.post(
        "/api/highlights/frames",
        json={
            "frame_type": "image",
            "media_url": "/api/media/x.webp",
            "audience_kind": "everyone",
        },
        headers=_auth(client),
    )
    assert r.status == 400

    # audience must be a list
    r = await client.post(
        "/api/highlights/frames",
        json={
            "frame_type": "image",
            "media_url": "/api/media/x.webp",
            "audience": "not-a-list",
        },
        headers=_auth(client),
    )
    assert r.status == 400


async def test_get_highlight_with_views_and_reactions(client):
    """Author GET returns inline views + reactions per frame."""
    resp = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    body = await resp.json()
    highlight_id = body["highlight"]["id"]
    frame_id = body["frame"]["id"]

    # React on the frame so the author gets a non-empty reactions block.
    r = await client.put(
        f"/api/highlights/frames/{frame_id}/reaction",
        json={"emoji": "🔥"},
        headers=_auth(client),
    )
    assert r.status == 200

    g = await client.get(f"/api/highlights/{highlight_id}", headers=_auth(client))
    assert g.status == 200
    detail = await g.json()
    assert detail["highlight"]["id"] == highlight_id
    assert "views" in detail
    assert "reactions" in detail
    assert frame_id in detail["reactions"]


async def test_get_unknown_highlight_returns_404(client):
    g = await client.get("/api/highlights/missing-id", headers=_auth(client))
    assert g.status == 404


async def test_reaction_validation(client):
    """Empty / missing emoji on PUT returns 400."""
    resp = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    frame_id = (await resp.json())["frame"]["id"]
    r = await client.put(
        f"/api/highlights/frames/{frame_id}/reaction",
        json={"emoji": "  "},
        headers=_auth(client),
    )
    assert r.status == 400
    r = await client.put(
        f"/api/highlights/frames/{frame_id}/reaction",
        json={},
        headers=_auth(client),
    )
    assert r.status == 400


async def test_view_marks_frame(client):
    """``POST /frames/{id}/view`` returns 200."""
    resp = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    frame_id = (await resp.json())["frame"]["id"]
    r = await client.post(
        f"/api/highlights/frames/{frame_id}/view",
        json={},
        headers=_auth(client),
    )
    assert r.status == 200


async def test_share_validates_scope(client):
    """``share`` must specify a known scope."""
    resp = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    highlight_id = (await resp.json())["highlight"]["id"]
    r = await client.post(
        f"/api/highlights/{highlight_id}/share",
        json={"scope": "neighbour"},
        headers=_auth(client),
    )
    assert r.status == 400


async def test_dm_reply_validates_body(client):
    """``dm-reply`` validates conversation_id + content."""
    resp = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    frame_id = (await resp.json())["frame"]["id"]
    # Missing conversation_id.
    r = await client.post(
        f"/api/highlights/frames/{frame_id}/dm-reply",
        json={"content": "lol"},
        headers=_auth(client),
    )
    assert r.status == 400
    # Non-string content.
    r = await client.post(
        f"/api/highlights/frames/{frame_id}/dm-reply",
        json={"conversation_id": "abc", "content": 123},
        headers=_auth(client),
    )
    assert r.status == 400


async def test_dm_reply_round_trip(client):
    """End-to-end: post a frame → reply via DM with the frame snapshot."""
    h = _auth(client)
    # Seed a peer user + open a 1:1 DM with them.
    await client._db.enqueue(
        "INSERT OR IGNORE INTO users(username, user_id, display_name, state) "
        "VALUES('bob', 'uid-bob', 'Bob', 'active')",
    )
    r = await client.post("/api/conversations/dm", json={"username": "bob"}, headers=h)
    assert r.status == 201
    conv_id = (await r.json())["id"]

    # Author posts a highlight frame, then replies-via-DM to it.
    r = await client.post(
        "/api/highlights/frames",
        json={
            "frame_type": "image",
            "media_url": "/api/media/x.webp",
            "caption_text": "from the trip",
            "caption_emoji": "🏖",
        },
        headers=h,
    )
    frame_id = (await r.json())["frame"]["id"]
    r = await client.post(
        f"/api/highlights/frames/{frame_id}/dm-reply",
        json={"conversation_id": conv_id, "content": "lol nice"},
        headers=h,
    )
    assert r.status == 201, await r.text()
    assert (await r.json())["message_id"]


async def test_share_to_space_round_trip(client):
    """Share a highlight into a space feed."""
    h = _auth(client)
    # Create a space first via the API so the user is its owner.
    r = await client.post(
        "/api/spaces",
        json={"name": "Trip Group", "space_type": "private"},
        headers=h,
    )
    assert r.status == 201, await r.text()
    space_id = (await r.json())["id"]
    r = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=h,
    )
    highlight_id = (await r.json())["highlight"]["id"]
    r = await client.post(
        f"/api/highlights/{highlight_id}/share",
        json={"scope": "space", "space_id": space_id, "note": "look"},
        headers=h,
    )
    # 201 (created) or 202 (queued for moderation) is acceptable.
    assert r.status in (201, 202), await r.text()


async def test_delete_frame_endpoint(client):
    """Author can DELETE a single frame."""
    resp = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    body = await resp.json()
    frame_id = body["frame"]["id"]
    r = await client.delete(
        f"/api/highlights/frames/{frame_id}",
        headers=_auth(client),
    )
    assert r.status == 204
