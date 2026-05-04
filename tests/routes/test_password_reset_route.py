"""Integration tests for the admin-issued password-reset flow.

* ``POST /api/admin/users/{username}/issue-password-reset`` (admin)
* ``POST /api/auth/redeem-password-reset`` (public)
"""

from __future__ import annotations

from socialhome.platform.standalone import StandaloneAdapter


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed_platform_user(client, username: str, password: str):
    hashed = StandaloneAdapter.hash_password(password)
    await client._db.enqueue(
        "INSERT INTO platform_users(username, display_name, password_hash) "
        "VALUES(?,?,?)",
        (username, username.title(), hashed),
    )
    # The route uses adapter.users.get(username) which checks the
    # ``users`` table — seed there too.
    await client._db.enqueue(
        "INSERT INTO users(user_id, username, display_name, is_admin) "
        "VALUES(?,?,?,?)",
        (username, username, username.title(), 0),
    )


# ── Admin issue endpoint ─────────────────────────────────────────────────


async def test_issue_returns_token_and_expires_at(client):
    await _seed_platform_user(client, "alice", "hunter2")
    r = await client.post(
        "/api/admin/users/alice/issue-password-reset",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert isinstance(body["token"], str) and len(body["token"]) > 32
    assert body["username"] == "alice"
    assert body["expires_at"]


async def test_issue_unknown_user_404(client):
    r = await client.post(
        "/api/admin/users/ghost/issue-password-reset",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_issue_non_admin_403(client):
    await client._db.enqueue(
        "UPDATE users SET is_admin=0 WHERE username='admin'", (),
    )
    r = await client.post(
        "/api/admin/users/alice/issue-password-reset",
        headers=_auth(client._tok),
    )
    assert r.status == 403


# ── Public redeem endpoint ───────────────────────────────────────────────


async def test_redeem_rotates_password(client):
    await _seed_platform_user(client, "alice", "old-pass-123")
    issue = await client.post(
        "/api/admin/users/alice/issue-password-reset",
        headers=_auth(client._tok),
    )
    token = (await issue.json())["token"]
    # Redeem with the new password.
    r = await client.post(
        "/api/auth/redeem-password-reset",
        json={"token": token, "new_password": "fresh-pass-9"},
    )
    assert r.status == 204
    # Old password no longer works.
    r1 = await client.post(
        "/api/auth/token",
        json={"username": "alice", "password": "old-pass-123"},
    )
    assert r1.status == 401
    # New password works.
    r2 = await client.post(
        "/api/auth/token",
        json={"username": "alice", "password": "fresh-pass-9"},
    )
    assert r2.status == 200


async def test_redeem_already_used_410(client):
    await _seed_platform_user(client, "alice", "x")
    issue = await client.post(
        "/api/admin/users/alice/issue-password-reset",
        headers=_auth(client._tok),
    )
    token = (await issue.json())["token"]
    await client.post(
        "/api/auth/redeem-password-reset",
        json={"token": token, "new_password": "first-pass-1"},
    )
    r = await client.post(
        "/api/auth/redeem-password-reset",
        json={"token": token, "new_password": "second-pass-2"},
    )
    assert r.status == 410


async def test_redeem_unknown_token_410(client):
    r = await client.post(
        "/api/auth/redeem-password-reset",
        json={"token": "not-a-real-token", "new_password": "abcdefgh"},
    )
    assert r.status == 410


async def test_redeem_short_password_422(client):
    await _seed_platform_user(client, "alice", "x")
    issue = await client.post(
        "/api/admin/users/alice/issue-password-reset",
        headers=_auth(client._tok),
    )
    token = (await issue.json())["token"]
    r = await client.post(
        "/api/auth/redeem-password-reset",
        json={"token": token, "new_password": "short"},
    )
    assert r.status == 422


async def test_redeem_missing_fields_422(client):
    r = await client.post(
        "/api/auth/redeem-password-reset", json={},
    )
    assert r.status == 422
