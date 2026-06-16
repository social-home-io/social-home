"""Tests for POST /api/me/handle — set the caller's public @handle."""

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


async def test_set_handle_succeeds(client):
    """A user sets a free handle → 200 + the DB row is updated."""
    resp = await client.post(
        "/api/me/handle",
        json={"handle": "newhandle"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"handle": "newhandle"}

    row = await client._db.fetchone(
        "SELECT handle FROM users WHERE user_id=?", (client._admin_uid,)
    )
    assert row["handle"] == "newhandle"


async def test_set_handle_strips_whitespace(client):
    """Surrounding whitespace is trimmed before the set + in the response."""
    resp = await client.post(
        "/api/me/handle",
        json={"handle": "  spaced  "},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"handle": "spaced"}

    row = await client._db.fetchone(
        "SELECT handle FROM users WHERE user_id=?", (client._admin_uid,)
    )
    assert row["handle"] == "spaced"


async def test_set_handle_taken_returns_422(client):
    """Setting to an existing handle → 422."""
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
        "INSERT INTO users(username, user_id, display_name, handle, is_admin) VALUES(?,?,?,?,0)",
        ("bob", uid2, "Bob", "taken"),
    )
    resp = await client.post(
        "/api/me/handle",
        json={"handle": "taken"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422


async def test_set_handle_reserved_returns_422(client):
    """A reserved handle (e.g. 'admin') → 422."""
    resp = await client.post(
        "/api/me/handle",
        json={"handle": "admin"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422


async def test_set_handle_missing_returns_422(client):
    """Missing 'handle' field → 422."""
    resp = await client.post(
        "/api/me/handle",
        json={},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422


async def test_set_handle_empty_returns_422(client):
    """Empty / whitespace-only 'handle' → 422."""
    resp = await client.post(
        "/api/me/handle",
        json={"handle": "   "},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422


async def test_set_handle_ha_source_succeeds(client):
    """An HA-source user can set their handle (unlike username) → 200."""
    await client._db.enqueue(
        "UPDATE users SET source='ha' WHERE user_id=?", (client._admin_uid,)
    )
    resp = await client.post(
        "/api/me/handle",
        json={"handle": "newhandle"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"handle": "newhandle"}

    row = await client._db.fetchone(
        "SELECT handle FROM users WHERE user_id=?", (client._admin_uid,)
    )
    assert row["handle"] == "newhandle"


async def test_set_handle_unauthenticated_returns_401(client):
    """No auth → 401."""
    resp = await client.post("/api/me/handle", json={"handle": "newhandle"})
    assert resp.status == 401
