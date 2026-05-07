"""Tests for ``GET /api/calendars/invitees`` (§23.60).

The picker shows members of confirmed paired remote instances ONLY —
no local household members. The empty case (zero pairings) is a normal
state; the SPA renders an empty-state CTA.
"""

from __future__ import annotations

from .conftest import _auth


async def _seed_paired_instance(
    db,
    *,
    instance_id: str,
    display_name: str,
    status: str = "confirmed",
) -> None:
    await db.enqueue(
        """
        INSERT INTO remote_instances(
            id, display_name, remote_identity_pk,
            key_self_to_remote, key_remote_to_self,
            remote_inbox_url, local_inbox_id, status, source
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            instance_id,
            display_name,
            "ab" * 32,
            "00",
            "00",
            f"https://{instance_id}.example/inbox/x",
            instance_id + "_local",
            status,
            "manual",
        ),
    )


async def _seed_remote_user(
    db,
    *,
    instance_id: str,
    user_id: str,
    username: str,
    display_name: str,
    deprovisioned: bool = False,
) -> None:
    await db.enqueue(
        """
        INSERT INTO remote_users(
            user_id, instance_id, remote_username, display_name,
            deprovisioned_at
        ) VALUES(?,?,?,?,?)
        """,
        (
            user_id,
            instance_id,
            username,
            display_name,
            "2026-01-01T00:00:00Z" if deprovisioned else None,
        ),
    )


async def test_invitees_empty_when_no_paired_instances(client):
    """Zero pairings → empty ``instances`` array (NOT 404)."""
    r = await client.get("/api/calendars/invitees", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body == {"instances": []}


async def test_invitees_lists_confirmed_instance_members(client):
    """Confirmed peer + member → one group with that member."""
    await _seed_paired_instance(client._db, instance_id="i1", display_name="Smith Home")
    await _seed_remote_user(
        client._db,
        instance_id="i1",
        user_id="u-bob",
        username="bob",
        display_name="Bob",
    )
    r = await client.get("/api/calendars/invitees", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert len(body["instances"]) == 1
    inst = body["instances"][0]
    assert inst["instance_id"] == "i1"
    assert inst["instance_name"] == "Smith Home"
    assert len(inst["members"]) == 1
    assert inst["members"][0]["user_id"] == "u-bob"
    assert inst["members"][0]["display_name"] == "Bob"


async def test_invitees_excludes_pending_and_unpairing_instances(client):
    """Only ``status='confirmed'`` instances surface."""
    await _seed_paired_instance(
        client._db,
        instance_id="i_pending",
        display_name="Pending Home",
        status="pending_sent",
    )
    await _seed_remote_user(
        client._db,
        instance_id="i_pending",
        user_id="u-pending",
        username="x",
        display_name="X",
    )
    r = await client.get("/api/calendars/invitees", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body == {"instances": []}


async def test_invitees_excludes_deprovisioned_remote_users(client):
    """Members soft-deleted on the peer side don't appear."""
    await _seed_paired_instance(client._db, instance_id="i1", display_name="Smith Home")
    await _seed_remote_user(
        client._db,
        instance_id="i1",
        user_id="u-active",
        username="alice",
        display_name="Alice",
    )
    await _seed_remote_user(
        client._db,
        instance_id="i1",
        user_id="u-gone",
        username="ghost",
        display_name="Ghost",
        deprovisioned=True,
    )
    r = await client.get("/api/calendars/invitees", headers=_auth(client._tok))
    body = await r.json()
    assert len(body["instances"]) == 1
    members = body["instances"][0]["members"]
    assert [m["user_id"] for m in members] == ["u-active"]


async def test_invitees_does_not_include_local_household_members(client):
    """Local users coordinate via the calendar selector — never the
    invite picker. Even with a confirmed peer, only that peer's
    members appear, never local rows."""
    # Seed a local member alongside the admin from the fixture.
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("lina", "u-lina-local", "Lina"),
    )
    await _seed_paired_instance(client._db, instance_id="i1", display_name="Smith Home")
    await _seed_remote_user(
        client._db,
        instance_id="i1",
        user_id="u-bob",
        username="bob",
        display_name="Bob",
    )
    r = await client.get("/api/calendars/invitees", headers=_auth(client._tok))
    body = await r.json()
    all_user_ids = [m["user_id"] for inst in body["instances"] for m in inst["members"]]
    assert all_user_ids == ["u-bob"]
    assert "u-lina-local" not in all_user_ids


async def test_invitees_requires_auth(client):
    """Unauthenticated calls 401."""
    r = await client.get("/api/calendars/invitees")
    assert r.status == 401
