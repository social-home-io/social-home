"""Tests for ``GET /api/admin/federation/compat`` (admin federation panel).

Admin-only list of confirmed peers with their advertised proto_version,
the features they lack vs OURS, and whether they've ever advertised
capabilities (NULL ``capabilities_seen_at`` ⇒ never).
"""

from __future__ import annotations

from socialhome.domain.federation_capabilities import OURS, features_missing_below

from .conftest import _auth


async def _seed_peer(
    db,
    *,
    instance_id: str,
    display_name: str,
    proto_version: int,
    status: str = "confirmed",
    capabilities_seen_at: str | None = None,
    last_reachable_at: str | None = None,
) -> None:
    await db.enqueue(
        """
        INSERT INTO remote_instances(
            id, display_name, remote_identity_pk,
            key_self_to_remote, key_remote_to_self,
            remote_inbox_url, local_inbox_id, status, source,
            proto_version, capabilities_seen_at, last_reachable_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
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
            proto_version,
            capabilities_seen_at,
            last_reachable_at,
        ),
    )


async def test_compat_requires_admin(client):
    """A non-admin token gets 403."""
    db = client._db
    # Demote the seeded admin user.
    await db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?",
        (client._uid,),
    )
    resp = await client.get("/api/admin/federation/compat", headers=_auth(client._tok))
    assert resp.status == 403


async def test_compat_lists_peer_below_ours(client):
    """A confirmed peer below OURS reports non-empty lacking_features and
    capabilities_known reflecting its capabilities_seen_at stamp."""
    db = client._db
    await _seed_peer(
        db,
        instance_id="peer-old",
        display_name="Old House",
        proto_version=13,
        capabilities_seen_at="2026-06-01T00:00:00+00:00",
        last_reachable_at="2026-06-02 10:00:00",
    )
    resp = await client.get("/api/admin/federation/compat", headers=_auth(client._tok))
    assert resp.status == 200
    body = await resp.json()
    assert body["ours"] == OURS
    peers = {p["instance_id"]: p for p in body["peers"]}
    p = peers["peer-old"]
    assert p["display_name"] == "Old House"
    assert p["proto_version"] == 13
    assert p["status"] == "confirmed"
    assert p["last_reachable_at"] == "2026-06-02 10:00:00"
    assert p["capabilities_known"] is True
    assert p["lacking_features"] == features_missing_below(13)
    assert p["lacking_features"]  # non-empty


async def test_compat_peer_at_ours_lacks_nothing(client):
    """A confirmed peer at OURS reports an empty lacking_features list."""
    db = client._db
    await _seed_peer(
        db,
        instance_id="peer-current",
        display_name="Current House",
        proto_version=OURS,
        capabilities_seen_at="2026-06-03T00:00:00+00:00",
    )
    resp = await client.get("/api/admin/federation/compat", headers=_auth(client._tok))
    body = await resp.json()
    peers = {p["instance_id"]: p for p in body["peers"]}
    assert peers["peer-current"]["lacking_features"] == []


async def test_compat_capabilities_known_distinguishes_never_advertised(client):
    """A NULL capabilities_seen_at ⇒ capabilities_known false (never
    advertised) vs a stamped peer ⇒ true."""
    db = client._db
    await _seed_peer(
        db,
        instance_id="peer-fresh",
        display_name="Fresh Pair",
        proto_version=1,
        capabilities_seen_at=None,
    )
    await _seed_peer(
        db,
        instance_id="peer-seen",
        display_name="Seen Pair",
        proto_version=1,
        capabilities_seen_at="2026-06-01T00:00:00+00:00",
    )
    resp = await client.get("/api/admin/federation/compat", headers=_auth(client._tok))
    body = await resp.json()
    peers = {p["instance_id"]: p for p in body["peers"]}
    assert peers["peer-fresh"]["capabilities_known"] is False
    assert peers["peer-seen"]["capabilities_known"] is True


async def test_compat_excludes_pending_peers(client):
    """Only confirmed peers appear — a pending pair is not listed."""
    db = client._db
    await _seed_peer(
        db,
        instance_id="peer-pending",
        display_name="Pending",
        proto_version=1,
        status="pending_sent",
    )
    resp = await client.get("/api/admin/federation/compat", headers=_auth(client._tok))
    body = await resp.json()
    ids = {p["instance_id"] for p in body["peers"]}
    assert "peer-pending" not in ids


# ── POST /api/admin/federation/resync ───────────────────────────────


async def test_resync_requires_admin(client):
    """A non-admin token gets 403."""
    db = client._db
    await db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?",
        (client._uid,),
    )
    resp = await client.post(
        "/api/admin/federation/resync",
        headers=_auth(client._tok),
        json={"instance_id": "peer-x", "scope": "capabilities"},
    )
    assert resp.status == 403


async def test_resync_rejects_bad_scope(client):
    """An unrecognised scope is 400 before any peer lookup."""
    resp = await client.post(
        "/api/admin/federation/resync",
        headers=_auth(client._tok),
        json={"instance_id": "peer-x", "scope": "nonsense"},
    )
    assert resp.status == 400


async def test_resync_rejects_empty_instance(client):
    """A missing instance_id is 400."""
    resp = await client.post(
        "/api/admin/federation/resync",
        headers=_auth(client._tok),
        json={"instance_id": "", "scope": "capabilities"},
    )
    assert resp.status == 400


async def test_resync_rejects_space_scope_without_id(client):
    """``space:`` with an empty id is 400."""
    resp = await client.post(
        "/api/admin/federation/resync",
        headers=_auth(client._tok),
        json={"instance_id": "peer-x", "scope": "space:"},
    )
    assert resp.status == 400


async def test_resync_peer_too_old_is_409(client):
    """A confirmed peer below v_19 can't honour the request → 409."""
    db = client._db
    await _seed_peer(
        db,
        instance_id="peer-old",
        display_name="Old House",
        proto_version=13,
        capabilities_seen_at="2026-06-01T00:00:00+00:00",
    )
    resp = await client.post(
        "/api/admin/federation/resync",
        headers=_auth(client._tok),
        json={"instance_id": "peer-old", "scope": "capabilities"},
    )
    assert resp.status == 409


async def test_resync_unknown_peer_is_409(client):
    """An unknown peer (peer_supports False) → 409."""
    resp = await client.post(
        "/api/admin/federation/resync",
        headers=_auth(client._tok),
        json={"instance_id": "no-such-peer", "scope": "capabilities"},
    )
    assert resp.status == 409


async def test_resync_v19_peer_capabilities_is_200(client):
    """A confirmed v_19 peer accepts the resync request → 200."""
    db = client._db
    await _seed_peer(
        db,
        instance_id="peer-new",
        display_name="New House",
        proto_version=OURS,
        capabilities_seen_at="2026-06-04T00:00:00+00:00",
    )
    resp = await client.post(
        "/api/admin/federation/resync",
        headers=_auth(client._tok),
        json={"instance_id": "peer-new", "scope": "capabilities"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {
        "status": "ok",
        "instance_id": "peer-new",
        "scope": "capabilities",
    }


async def test_resync_v19_peer_space_scope_is_200(client):
    """A ``space:<id>`` scope against a v_19 peer → 200."""
    db = client._db
    await _seed_peer(
        db,
        instance_id="peer-new2",
        display_name="New House 2",
        proto_version=OURS,
        capabilities_seen_at="2026-06-04T00:00:00+00:00",
    )
    resp = await client.post(
        "/api/admin/federation/resync",
        headers=_auth(client._tok),
        json={"instance_id": "peer-new2", "scope": "space:sp-1"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["scope"] == "space:sp-1"
