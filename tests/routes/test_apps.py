"""HTTP tests for /api/apps, /api/apps/catalog, and /api/apps/{app_id}."""

from __future__ import annotations

from unittest.mock import AsyncMock

from socialhome.auth import sha256_token_hash
from socialhome.domain.apps import (
    AppAlreadyInstalledError,
    AppCatalogEntry,
    AppIntegrityError,
    AppManifest,
    AppNotFoundError,
    AppNotEnabledError,
    InstalledApp,
)
from socialhome.repositories.app_repo import SqliteAppRepo
from socialhome.services.app_service import AppService

from .conftest import _auth


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_installed(
    app_id: str = "com.example.hello",
    enabled: bool = True,
) -> InstalledApp:
    manifest = AppManifest(entry="index.js", icon="icon.png", capabilities=("read",))
    return InstalledApp(
        app_id=app_id,
        name="Hello App",
        version="1.0.0",
        enabled=enabled,
        manifest=manifest,
        bundle_path=f"apps/{app_id}/1.0.0",
        bundle_sha256="abc123",
        source_url="https://example.com/hello.tgz",
        installed_by="admin",
        installed_at="2026-06-01T00:00:00+00:00",
    )


def _make_catalog_entry(app_id: str = "com.example.new") -> AppCatalogEntry:
    return AppCatalogEntry(
        app_id=app_id,
        name="New App",
        latest_version="2.0.0",
        description="A test app",
        icon_url="https://example.com/icon.png",
        capabilities=("read", "write"),
        bundle_url="https://example.com/new.tgz",
        bundle_sha256="def456",
    )


async def _seed_member(db, username: str = "bob", user_id: str = "bob-id") -> str:
    """Seed a non-admin user and return their raw token."""
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        (username, user_id, username.capitalize()),
    )
    raw = f"{username}-raw-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        (f"tok-{username}", user_id, "t", sha256_token_hash(raw)),
    )
    return raw


# ── GET /api/apps ─────────────────────────────────────────────────────────


async def test_list_apps_empty_as_admin(client):
    r = await client.get("/api/apps", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body == {"apps": []}


async def test_list_apps_empty_as_member(client):
    """Non-admins can list apps too, but only enabled ones (empty → empty)."""
    member_tok = await _seed_member(client._db)
    r = await client.get("/api/apps", headers=_auth(member_tok))
    assert r.status == 200
    body = await r.json()
    assert body == {"apps": []}


async def test_list_apps_requires_auth(client):
    r = await client.get("/api/apps")
    assert r.status == 401


async def test_list_apps_admin_sees_disabled(client, monkeypatch):
    """Admin sees all apps including disabled ones."""
    disabled_app = _make_installed(enabled=False)
    monkeypatch.setattr(
        AppService, "list_installed", AsyncMock(return_value=[disabled_app])
    )

    r = await client.get("/api/apps", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert len(body["apps"]) == 1
    assert body["apps"][0]["enabled"] is False


async def test_list_apps_member_filters_disabled(client, monkeypatch):
    """Non-admin can see only enabled apps."""
    enabled_app = _make_installed(app_id="com.example.enabled", enabled=True)
    disabled_app = _make_installed(app_id="com.example.disabled", enabled=False)
    monkeypatch.setattr(
        AppService,
        "list_installed",
        AsyncMock(return_value=[enabled_app, disabled_app]),
    )

    member_tok = await _seed_member(client._db)
    r = await client.get("/api/apps", headers=_auth(member_tok))
    assert r.status == 200
    body = await r.json()
    ids = [a["app_id"] for a in body["apps"]]
    assert "com.example.enabled" in ids
    assert "com.example.disabled" not in ids


# ── POST /api/apps ────────────────────────────────────────────────────────


async def test_install_requires_admin(client):
    """Non-admin POST → 403."""
    member_tok = await _seed_member(client._db)
    r = await client.post(
        "/api/apps",
        json={"app_id": "com.example.hello"},
        headers=_auth(member_tok),
    )
    assert r.status == 403


async def test_install_requires_auth(client):
    r = await client.post("/api/apps", json={"app_id": "com.example.hello"})
    assert r.status == 401


async def test_install_rejects_missing_app_id(client):
    r = await client.post("/api/apps", json={}, headers=_auth(client._tok))
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_install_rejects_empty_app_id(client):
    r = await client.post("/api/apps", json={"app_id": ""}, headers=_auth(client._tok))
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_install_rejects_non_string_app_id(client):
    r = await client.post("/api/apps", json={"app_id": 123}, headers=_auth(client._tok))
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_install_not_found_returns_404(client, monkeypatch):
    monkeypatch.setattr(
        AppService,
        "install",
        AsyncMock(side_effect=AppNotFoundError("not in catalog")),
    )
    r = await client.post(
        "/api/apps",
        json={"app_id": "com.example.missing"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_install_already_installed_returns_409(client, monkeypatch):
    monkeypatch.setattr(
        AppService,
        "install",
        AsyncMock(side_effect=AppAlreadyInstalledError("already there")),
    )
    r = await client.post(
        "/api/apps",
        json={"app_id": "com.example.hello"},
        headers=_auth(client._tok),
    )
    assert r.status == 409
    body = await r.json()
    assert body["error"]["code"] == "CONFLICT"


async def test_install_integrity_error_returns_400(client, monkeypatch):
    monkeypatch.setattr(
        AppService,
        "install",
        AsyncMock(side_effect=AppIntegrityError("sha256 mismatch")),
    )
    r = await client.post(
        "/api/apps",
        json={"app_id": "com.example.hello"},
        headers=_auth(client._tok),
    )
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_install_success_returns_201(client, monkeypatch):
    app = _make_installed()
    monkeypatch.setattr(AppService, "install", AsyncMock(return_value=app))

    r = await client.post(
        "/api/apps",
        json={"app_id": "com.example.hello"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["app_id"] == "com.example.hello"
    assert body["name"] == "Hello App"
    assert body["version"] == "1.0.0"
    assert body["enabled"] is True
    assert body["capabilities"] == ["read"]
    assert body["icon"] == "icon.png"


# ── GET /api/apps/catalog ────────────────────────────────────────────────


async def test_catalog_is_admin_only(client):
    member_tok = await _seed_member(client._db)
    r = await client.get("/api/apps/catalog", headers=_auth(member_tok))
    assert r.status == 403


async def test_catalog_requires_auth(client):
    r = await client.get("/api/apps/catalog")
    assert r.status == 401


async def test_admin_can_list_catalog_empty(client, monkeypatch):
    """When no catalog is configured, browse_catalog returns an empty list."""
    monkeypatch.setattr(AppService, "browse_catalog", AsyncMock(return_value=[]))
    r = await client.get("/api/apps/catalog", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body == {"apps": []}


async def test_admin_can_list_catalog_with_entry(client, monkeypatch):
    entry = _make_catalog_entry()
    monkeypatch.setattr(AppService, "browse_catalog", AsyncMock(return_value=[entry]))

    r = await client.get("/api/apps/catalog", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert len(body["apps"]) == 1
    item = body["apps"][0]
    assert item["app_id"] == "com.example.new"
    assert item["name"] == "New App"
    assert item["latest_version"] == "2.0.0"
    assert item["description"] == "A test app"
    assert item["icon_url"] == "https://example.com/icon.png"
    assert item["capabilities"] == ["read", "write"]


# ── GET /api/apps/{app_id} ───────────────────────────────────────────────


async def test_detail_404_for_unknown(client):
    r = await client.get("/api/apps/nope", headers=_auth(client._tok))
    assert r.status == 404
    body = await r.json()
    assert body["error"]["code"] == "NOT_FOUND"


async def test_detail_requires_auth(client):
    r = await client.get("/api/apps/some-app")
    assert r.status == 401


async def test_detail_member_404_for_disabled(client, monkeypatch):
    disabled_app = _make_installed(enabled=False)
    monkeypatch.setattr(AppService, "get", AsyncMock(return_value=disabled_app))

    member_tok = await _seed_member(client._db)
    r = await client.get("/api/apps/com.example.hello", headers=_auth(member_tok))
    assert r.status == 404


async def test_detail_admin_sees_disabled(client, monkeypatch):
    disabled_app = _make_installed(enabled=False)
    monkeypatch.setattr(AppService, "get", AsyncMock(return_value=disabled_app))

    r = await client.get("/api/apps/com.example.hello", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body["enabled"] is False


async def test_detail_member_sees_enabled(client, monkeypatch):
    enabled_app = _make_installed(enabled=True)
    monkeypatch.setattr(AppService, "get", AsyncMock(return_value=enabled_app))

    member_tok = await _seed_member(client._db)
    r = await client.get("/api/apps/com.example.hello", headers=_auth(member_tok))
    assert r.status == 200
    body = await r.json()
    assert body["enabled"] is True


# ── PATCH /api/apps/{app_id} ──────────────────────────────────────────────


async def test_patch_requires_admin(client):
    member_tok = await _seed_member(client._db)
    r = await client.patch(
        "/api/apps/com.example.hello",
        json={"enabled": False},
        headers=_auth(member_tok),
    )
    assert r.status == 403


async def test_patch_requires_auth(client):
    r = await client.patch("/api/apps/com.example.hello", json={"enabled": False})
    assert r.status == 401


async def test_patch_rejects_non_bool_enabled(client):
    r = await client.patch(
        "/api/apps/com.example.hello",
        json={"enabled": "yes"},
        headers=_auth(client._tok),
    )
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_patch_missing_enabled_returns_400(client):
    r = await client.patch(
        "/api/apps/com.example.hello",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_patch_not_found_returns_404(client, monkeypatch):
    monkeypatch.setattr(
        AppService,
        "set_enabled",
        AsyncMock(side_effect=AppNotFoundError("not installed")),
    )
    r = await client.patch(
        "/api/apps/com.example.missing",
        json={"enabled": True},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_patch_set_enabled_success(client, monkeypatch):
    updated = _make_installed(enabled=False)
    monkeypatch.setattr(AppService, "set_enabled", AsyncMock(return_value=updated))

    r = await client.patch(
        "/api/apps/com.example.hello",
        json={"enabled": False},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["enabled"] is False


# ── DELETE /api/apps/{app_id} ─────────────────────────────────────────────


async def test_delete_requires_admin(client):
    member_tok = await _seed_member(client._db)
    r = await client.delete(
        "/api/apps/com.example.hello",
        headers=_auth(member_tok),
    )
    assert r.status == 403


async def test_delete_requires_auth(client):
    r = await client.delete("/api/apps/com.example.hello")
    assert r.status == 401


async def test_delete_not_found_returns_404(client, monkeypatch):
    monkeypatch.setattr(
        AppService,
        "uninstall",
        AsyncMock(side_effect=AppNotFoundError("not installed")),
    )
    r = await client.delete(
        "/api/apps/com.example.missing",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_delete_success_returns_ok(client, monkeypatch):
    monkeypatch.setattr(AppService, "uninstall", AsyncMock(return_value=None))

    r = await client.delete(
        "/api/apps/com.example.hello",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body == {"status": "ok"}


# ── Store helpers ─────────────────────────────────────────────────────────


def _make_installed_no_installer(
    app_id: str = "com.example.hello",
    enabled: bool = True,
) -> InstalledApp:
    """Like ``_make_installed`` but with ``installed_by=None`` to avoid FK issues."""
    manifest = AppManifest(entry="index.js", icon="icon.png", capabilities=("read",))
    return InstalledApp(
        app_id=app_id,
        name="Hello App",
        version="1.0.0",
        enabled=enabled,
        manifest=manifest,
        bundle_path=f"apps/{app_id}/1.0.0",
        bundle_sha256="abc123",
        source_url="https://example.com/hello.tgz",
        installed_by=None,
        installed_at="2026-06-01T00:00:00+00:00",
    )


async def _seed_installed_enabled_app(db, app_id: str = "com.example.hello") -> None:
    """Insert an installed+enabled app row directly via SqliteAppRepo.

    Uses ``installed_by=None`` to avoid a FK constraint on the users table
    (the installer user may not have been seeded yet at call time).
    """
    repo = SqliteAppRepo(db)
    app = _make_installed_no_installer(app_id=app_id, enabled=True)
    await repo.install(app)


async def _seed_second_user(
    db, username: str = "carol", user_id: str = "carol-id"
) -> str:
    """Seed a second non-admin user and return their raw token."""
    return await _seed_member(db, username=username, user_id=user_id)


# ── GET /api/apps/{app_id}/store ────────────────────────────────────────


async def test_store_list_empty_for_new_user(client):
    """GET /store returns empty items dict when no keys have been set."""
    await _seed_installed_enabled_app(client._db)
    r = await client.get(
        "/api/apps/com.example.hello/store",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body == {"items": {}}


async def test_store_list_requires_auth(client):
    await _seed_installed_enabled_app(client._db)
    r = await client.get("/api/apps/com.example.hello/store")
    assert r.status == 401


# ── PUT + GET /api/apps/{app_id}/store/{key} ────────────────────────────


async def test_store_put_then_get(client):
    """PUT a value then GET it back → 200 with the same value."""
    await _seed_installed_enabled_app(client._db)

    value = {"turn": "w"}
    r = await client.put(
        "/api/apps/com.example.hello/store/game1",
        json={"value": value},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    put_body = await r.json()
    assert put_body == {"key": "game1", "value": value}

    r2 = await client.get(
        "/api/apps/com.example.hello/store/game1",
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    get_body = await r2.json()
    assert get_body == {"key": "game1", "value": value}


async def test_store_get_missing_key_404(client):
    """GET a key that was never set → 404 NOT_FOUND."""
    await _seed_installed_enabled_app(client._db)
    r = await client.get(
        "/api/apps/com.example.hello/store/no-such-key",
        headers=_auth(client._tok),
    )
    assert r.status == 404
    body = await r.json()
    assert body["error"]["code"] == "NOT_FOUND"


async def test_store_on_uninstalled_app_404(client):
    """Store op on an app_id not installed → 404 via AppNotFoundError."""
    r = await client.get(
        "/api/apps/com.example.not-installed/store/key",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_store_list_scoped_to_caller(client):
    """User A's PUT does NOT appear in user B's GET /store.

    We seed a second user (carol) and verify that after user A (admin) PUTs
    a key, user B's store list is empty — store data is per-user-scoped.
    """
    await _seed_installed_enabled_app(client._db)

    # User A (admin) puts a key
    r = await client.put(
        "/api/apps/com.example.hello/store/shared-key",
        json={"value": "user-a-data"},
        headers=_auth(client._tok),
    )
    assert r.status == 200

    # User B (carol) lists their own store — must be empty
    carol_tok = await _seed_second_user(client._db)
    r2 = await client.get(
        "/api/apps/com.example.hello/store",
        headers=_auth(carol_tok),
    )
    assert r2.status == 200
    body = await r2.json()
    # carol's store has no keys — user A's data is invisible
    assert body == {"items": {}}


async def test_store_value_too_large_413(client):
    """PUT a value exceeding the byte quota → 413 QUOTA_EXCEEDED."""
    await _seed_installed_enabled_app(client._db)

    big_value = {"x": "a" * 70000}
    r = await client.put(
        "/api/apps/com.example.hello/store/big-key",
        json={"value": big_value},
        headers=_auth(client._tok),
    )
    assert r.status == 413
    body = await r.json()
    assert body["error"]["code"] == "QUOTA_EXCEEDED"


async def test_store_delete(client):
    """PUT then DELETE then GET → 404 NOT_FOUND."""
    await _seed_installed_enabled_app(client._db)

    # PUT the key
    await client.put(
        "/api/apps/com.example.hello/store/temp-key",
        json={"value": "to-be-deleted"},
        headers=_auth(client._tok),
    )

    # DELETE
    r_del = await client.delete(
        "/api/apps/com.example.hello/store/temp-key",
        headers=_auth(client._tok),
    )
    assert r_del.status == 200
    body = await r_del.json()
    assert body == {"status": "ok"}

    # GET after delete → 404
    r_get = await client.get(
        "/api/apps/com.example.hello/store/temp-key",
        headers=_auth(client._tok),
    )
    assert r_get.status == 404


async def test_store_on_disabled_app_403(client, monkeypatch):
    """Store op on a disabled app → 403 FORBIDDEN (AppNotEnabledError)."""
    await _seed_installed_enabled_app(client._db)
    monkeypatch.setattr(
        AppService,
        "store_get",
        AsyncMock(side_effect=AppNotEnabledError("App disabled")),
    )
    r = await client.get(
        "/api/apps/com.example.hello/store/any-key",
        headers=_auth(client._tok),
    )
    assert r.status == 403
    body = await r.json()
    assert body["error"]["code"] == "FORBIDDEN"


async def test_store_put_missing_value_field_400(client):
    """PUT without a 'value' field → 400 UNPROCESSABLE."""
    await _seed_installed_enabled_app(client._db)
    r = await client.put(
        "/api/apps/com.example.hello/store/some-key",
        json={"not_value": 42},
        headers=_auth(client._tok),
    )
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"
