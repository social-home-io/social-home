"""HTTP smoke tests for the stories routes."""

from __future__ import annotations


def _auth(client) -> dict:
    return {"Authorization": f"Bearer {client._tok}"}


async def test_create_frame_then_list(client):
    """POST /api/stories/frames creates a story; GET /api/stories returns it."""
    resp = await client.post(
        "/api/stories/frames",
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
    assert body["story"]["author_user_id"] == client._uid
    assert body["frame"]["sequence"] == 1
    assert body["frame"]["caption_emoji"] == "🌅"

    listed = await client.get("/api/stories", headers=_auth(client))
    assert listed.status == 200
    rows = await listed.json()
    assert len(rows) == 1
    assert len(rows[0]["frames"]) == 1


async def test_react_and_clear(client):
    """PUT /reaction sets, DELETE /reaction clears."""
    resp = await client.post(
        "/api/stories/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    assert resp.status == 201
    body = await resp.json()
    frame_id = body["frame"]["id"]
    # Authors don't accumulate views on their own stories, but reactions
    # are still recorded — we just want to exercise the endpoints.
    r = await client.put(
        f"/api/stories/frames/{frame_id}/reaction",
        json={"emoji": "🔥"},
        headers=_auth(client),
    )
    assert r.status == 200
    r = await client.delete(
        f"/api/stories/frames/{frame_id}/reaction",
        headers=_auth(client),
    )
    assert r.status == 200


async def test_share_to_household_feed(client):
    """POST /api/stories/{id}/share creates a story_share post."""
    resp = await client.post(
        "/api/stories/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    body = await resp.json()
    story_id = body["story"]["id"]
    s = await client.post(
        f"/api/stories/{story_id}/share",
        json={"scope": "household", "note": "look at this"},
        headers=_auth(client),
    )
    assert s.status == 201, await s.text()
    j = await s.json()
    assert j["story_id"] == story_id
    assert j["post_id"]


async def test_delete_story(client):
    resp = await client.post(
        "/api/stories/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    story_id = (await resp.json())["story"]["id"]
    d = await client.delete(f"/api/stories/{story_id}", headers=_auth(client))
    assert d.status == 204
    listed = await client.get("/api/stories", headers=_auth(client))
    rows = await listed.json()
    assert rows == []
