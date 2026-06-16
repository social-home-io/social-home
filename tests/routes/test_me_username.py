"""Tests for POST /api/me/username — rename the caller's login username."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.app import create_app
from socialhome.app_keys import db_key as _db_key
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client(tmp_dir):
    """App client with an admin user (pascal, source='manual')."""
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        db = app[_db_key]
        _row = await db.fetchone(
            "SELECT identity_public_key FROM instance_identity WHERE id='self'"
        )
        _pk = bytes.fromhex(_row["identity_public_key"])
        uid = derive_user_id(_pk, "pascal")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,1)",
            ("pascal", uid, "Pascal"),
        )
        raw_token = "test-token-raw"
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-1", uid, "test", sha256_token_hash(raw_token)),
        )
        tc._admin_token = raw_token
        tc._admin_uid = uid
        tc._db = db
        yield tc


async def test_rename_username_succeeds(client):
    """A manual-source user renames to a free name → 200 + the DB row moves."""
    resp = await client.post(
        "/api/me/username",
        json={"username": "newname"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"username": "newname"}

    row = await client._db.fetchone(
        "SELECT username FROM users WHERE user_id=?", (client._admin_uid,)
    )
    assert row["username"] == "newname"


async def test_rename_username_strips_whitespace(client):
    """Surrounding whitespace is trimmed before the rename + in the response."""
    resp = await client.post(
        "/api/me/username",
        json={"username": "  spaced  "},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"username": "spaced"}


async def test_rename_username_taken_returns_422(client):
    """Renaming to an existing username → 422."""
    uid2 = derive_user_id(
        bytes.fromhex(
            (
                await client._db.fetchone(
                    "SELECT identity_public_key FROM instance_identity WHERE id='self'"
                )
            )["identity_public_key"]
        ),
        "bob",
    )
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("bob", uid2, "Bob"),
    )
    resp = await client.post(
        "/api/me/username",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422


async def test_rename_username_too_long_returns_422(client):
    """A username over 32 chars → 422."""
    resp = await client.post(
        "/api/me/username",
        json={"username": "x" * 33},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422


async def test_rename_username_reserved_returns_422(client):
    """A reserved username (e.g. 'admin') → 422."""
    resp = await client.post(
        "/api/me/username",
        json={"username": "admin"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422


async def test_rename_username_missing_returns_422(client):
    """Missing 'username' field → 422."""
    resp = await client.post(
        "/api/me/username",
        json={},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422


async def test_rename_username_empty_returns_422(client):
    """Empty / whitespace-only 'username' → 422."""
    resp = await client.post(
        "/api/me/username",
        json={"username": "   "},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422


async def test_rename_username_ha_source_returns_403(client):
    """An HA-source user's name is owned by HA → 403."""
    await client._db.enqueue(
        "UPDATE users SET source='ha' WHERE user_id=?", (client._admin_uid,)
    )
    resp = await client.post(
        "/api/me/username",
        json={"username": "newname"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 403


async def test_rename_username_unauthenticated_returns_401(client):
    """No auth → 401."""
    resp = await client.post("/api/me/username", json={"username": "newname"})
    assert resp.status == 401
