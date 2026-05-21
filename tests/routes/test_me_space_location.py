"""HTTP tests for ``GET /api/me/space-location-sharing``."""

from __future__ import annotations

from .conftest import _auth


async def _create_space(client, *, name: str = "Test Space") -> str:
    """Create a space via the API and return its id."""
    resp = await client.post(
        "/api/spaces",
        json={"name": name, "join_mode": "open"},
        headers=_auth(client._tok),
    )
    assert resp.status == 201, await resp.text()
    return (await resp.json())["id"]


async def _enable_location_feature(client, space_id: str, *, enabled: bool) -> None:
    await client._db.enqueue(
        "UPDATE spaces SET feature_location=? WHERE id=?",
        (1 if enabled else 0, space_id),
    )


async def _set_member_opt_in(
    client, space_id: str, user_id: str, *, enabled: bool
) -> None:
    await client._db.enqueue(
        "UPDATE space_members SET location_share_enabled=? "
        "WHERE space_id=? AND user_id=?",
        (1 if enabled else 0, space_id, user_id),
    )


async def test_get_requires_auth(client):
    r = await client.get("/api/me/space-location-sharing")
    assert r.status == 401


async def test_get_returns_empty_when_no_spaces(client):
    r = await client.get(
        "/api/me/space-location-sharing",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body == {"spaces": []}


async def test_get_returns_empty_when_spaces_lack_location_feature(client):
    """Member of a space where feature_location=0 — not included."""
    space_id = await _create_space(client, name="NoFeat")
    await _enable_location_feature(client, space_id, enabled=False)

    r = await client.get(
        "/api/me/space-location-sharing",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body == {"spaces": []}


async def test_get_returns_spaces_with_location_feature(client):
    """Member of a space where feature_location=1 — returned with correct fields."""
    space_id = await _create_space(client, name="Family")
    await _enable_location_feature(client, space_id, enabled=True)
    # Set an emoji for the space
    await client._db.enqueue(
        "UPDATE spaces SET emoji=? WHERE id=?",
        ("🏡", space_id),
    )

    r = await client.get(
        "/api/me/space-location-sharing",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert len(body["spaces"]) == 1
    row = body["spaces"][0]
    assert row["space_id"] == space_id
    assert row["space_name"] == "Family"
    assert row["space_emoji"] == "🏡"
    # Default location_share_enabled is 0 in the schema
    assert isinstance(row["location_share_enabled"], bool)


async def test_get_respects_location_share_enabled_true(client):
    """location_share_enabled=1 returns True flag."""
    space_id = await _create_space(client, name="Alpha")
    await _enable_location_feature(client, space_id, enabled=True)
    await _set_member_opt_in(client, space_id, client._uid, enabled=True)

    r = await client.get(
        "/api/me/space-location-sharing",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert len(body["spaces"]) == 1
    assert body["spaces"][0]["location_share_enabled"] is True


async def test_get_respects_location_share_enabled_false(client):
    """location_share_enabled=0 returns False flag."""
    space_id = await _create_space(client, name="Beta")
    await _enable_location_feature(client, space_id, enabled=True)
    await _set_member_opt_in(client, space_id, client._uid, enabled=False)

    r = await client.get(
        "/api/me/space-location-sharing",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert len(body["spaces"]) == 1
    assert body["spaces"][0]["location_share_enabled"] is False


async def test_get_orders_by_space_name(client):
    """Multiple spaces are returned sorted alphabetically by name."""
    for name in ["Zulu", "Alpha", "Mike"]:
        sid = await _create_space(client, name=name)
        await _enable_location_feature(client, sid, enabled=True)

    r = await client.get(
        "/api/me/space-location-sharing",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    names = [s["space_name"] for s in body["spaces"]]
    assert names == ["Alpha", "Mike", "Zulu"]


async def test_get_excludes_spaces_caller_is_not_member_of(client):
    """After being removed from a space, it no longer appears in the list."""
    space_id = await _create_space(client, name="Former")
    await _enable_location_feature(client, space_id, enabled=True)
    # Confirm it shows up when admin is a member
    r1 = await client.get(
        "/api/me/space-location-sharing",
        headers=_auth(client._tok),
    )
    assert r1.status == 200
    assert len((await r1.json())["spaces"]) == 1

    # Now remove admin from the space
    await client._db.enqueue(
        "DELETE FROM space_members WHERE space_id=? AND user_id=?",
        (space_id, client._uid),
    )

    r2 = await client.get(
        "/api/me/space-location-sharing",
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    assert (await r2.json()) == {"spaces": []}
