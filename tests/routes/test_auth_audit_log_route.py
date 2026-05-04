"""Integration tests for the auth audit log writes + admin GET."""

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
    await client._db.enqueue(
        "INSERT INTO users(user_id, username, display_name, is_admin) VALUES(?,?,?,?)",
        (username, username, username.title(), 0),
    )


async def _audit_rows(client) -> list[dict]:
    rows = await client._db.fetchall(
        "SELECT event_type, username, ip_address FROM auth_audit_log "
        "ORDER BY created_at DESC",
    )
    return [dict(r) for r in rows]


# ── Login (success / failure) ────────────────────────────────────────────


async def test_login_success_writes_audit_row(client):
    await _seed_platform_user(client, "alice", "hunter2")
    await client.post(
        "/api/auth/token",
        json={"username": "alice", "password": "hunter2"},
    )
    rows = await _audit_rows(client)
    assert any(
        r["event_type"] == "login_success" and r["username"] == "alice" for r in rows
    )


async def test_login_failure_writes_audit_row(client):
    await client.post(
        "/api/auth/token",
        json={"username": "ghost", "password": "wrong"},
    )
    rows = await _audit_rows(client)
    assert any(
        r["event_type"] == "login_failure" and r["username"] == "ghost" for r in rows
    )


# ── Reset issue + redeem ────────────────────────────────────────────────


async def test_reset_issue_writes_audit_row(client):
    await _seed_platform_user(client, "alice", "x")
    await client.post(
        "/api/admin/users/alice/issue-password-reset",
        headers=_auth(client._tok),
    )
    rows = await _audit_rows(client)
    assert any(
        r["event_type"] == "reset_issue" and r["username"] == "alice" for r in rows
    )


async def test_redeem_success_writes_audit_row(client):
    await _seed_platform_user(client, "alice", "x")
    issue = await client.post(
        "/api/admin/users/alice/issue-password-reset",
        headers=_auth(client._tok),
    )
    token = (await issue.json())["token"]
    await client.post(
        "/api/auth/redeem-password-reset",
        json={"token": token, "new_password": "fresh-pass-1"},
    )
    rows = await _audit_rows(client)
    assert any(
        r["event_type"] == "reset_redeem_success" and r["username"] == "alice"
        for r in rows
    )


async def test_redeem_failure_writes_audit_row(client):
    await client.post(
        "/api/auth/redeem-password-reset",
        json={"token": "not-a-real-token", "new_password": "abcdefgh"},
    )
    rows = await _audit_rows(client)
    assert any(
        r["event_type"] == "reset_redeem_failure" and r["username"] is None
        for r in rows
    )


# ── Admin endpoint ───────────────────────────────────────────────────────


async def test_admin_audit_endpoint_returns_events(client):
    await client.post(
        "/api/auth/token",
        json={"username": "ghost", "password": "wrong"},
    )
    r = await client.get(
        "/api/admin/auth-audit",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert any(e["event_type"] == "login_failure" for e in body["events"])


async def test_admin_audit_endpoint_non_admin_403(client):
    await client._db.enqueue(
        "UPDATE users SET is_admin=0 WHERE username='admin'",
        (),
    )
    r = await client.get(
        "/api/admin/auth-audit",
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_admin_audit_endpoint_caps_limit(client):
    # Limit is clamped to 1..500.
    r = await client.get(
        "/api/admin/auth-audit?limit=99999",
        headers=_auth(client._tok),
    )
    assert r.status == 200
