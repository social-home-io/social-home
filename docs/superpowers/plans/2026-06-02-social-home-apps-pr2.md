# Social Home Apps — Implementation Plan (PR2: Per-User Storage API)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Give each installed app a per-user key-value store (`store("key", value)`), automatically purged when the app is uninstalled or the user is deleted, with quota caps so a buggy/hostile app can't fill the DB.

**Architecture:** Extend the PR1 `installed_apps` stack. New `app_kv` table keyed `(app_id, user_id, key)` with FK cascades. Extend `AbstractAppRepo`/`SqliteAppRepo` with KV methods (key is a bound `?` parameter), add quota enforcement + KV methods to `AppService`, expose `GET/PUT/DELETE /api/apps/{app_id}/store/{key}` + `GET /api/apps/{app_id}/store`, scoped server-side to the caller's `user_id`. (The `sh.store.*` JS SDK lands in PR3 with the sandbox bridge.)

**Tech Stack:** Python 3.14 / aiohttp / aiosqlite; mirrors the PR1 `app_repo` / `app_service` / `routes/apps.py` patterns. Builds on branch `feat/social-home-apps-pr1`.

---

## Decisions (locked in PR1 + brainstorm)
- **Per-user scope:** PK `(app_id, user_id, key)` — each member gets a private namespace.
- **Value shape:** the store holds arbitrary JSON. API body is `{"value": <any JSON>}`; server persists `json.dumps(value)`; reads return `{"key", "value": <parsed>}`. A raw string is just a JSON string value.
- **Quota:** per `(app_id, user_id)` — max keys + max bytes-per-value + max total bytes. Exceed → `AppQuotaExceededError` → HTTP 413.
- **App must be installed + enabled** to use its store: not installed → 404 `AppNotFoundError`; disabled → 403 `AppNotEnabledError`.
- **Cleanup:** FK `ON DELETE CASCADE` on both `app_id`→`installed_apps` and `user_id`→`users`. Uninstall (PR1 `AppService.uninstall`) already deletes the `installed_apps` row, so KV rows cascade automatically — add a regression test proving it.

---

## Quota constants (module-level in `services/app_service.py`)
```python
APP_KV_MAX_KEYS = 500            # per (app_id, user_id)
APP_KV_MAX_VALUE_BYTES = 64 * 1024   # 64 KiB per value (json-encoded)
APP_KV_MAX_KEY_LEN = 256
```

---

## Task 1: Migration — `app_kv` table

**Files:** Create `socialhome/migrations/0021_app_kv.sql`; Test: the existing migration-applies suite.

- [ ] **Step 1: Write the migration**

```sql
-- Social Home Apps: per-user key-value store for installed apps.
--
-- Migration audit (CLAUDE.md):
--   1. Audited the PR1 installed_apps shape + the preferences table. Neither
--      holds open-ended per-(app,user,key) data; preferences is a fixed
--      column set, installed_apps is one row per app. A dedicated table is
--      the only shape that gives per-app/per-user namespacing + FK-cascade
--      cleanup on uninstall and on user deletion.
--   2. Alternative rejected — a JSON blob on installed_apps would be
--      household-global (not per-user) and lose key-level reads/writes and
--      quota accounting; a column on preferences can't key by app.
--   3. Smallest change — additive CREATE TABLE with the two cascades; no
--      existing row touched.

CREATE TABLE app_kv (
    app_id     TEXT NOT NULL REFERENCES installed_apps(app_id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES users(user_id)          ON DELETE CASCADE,
    key        TEXT NOT NULL,
    value_json TEXT NOT NULL,        -- JSON-encoded value (any JSON type)
    updated_at TEXT NOT NULL,        -- UTC ISO 8601
    PRIMARY KEY (app_id, user_id, key)
);
```

- [ ] **Step 2: Verify** — `pytest tests/db/test_migrations.py tests/db/test_database.py::test_startup_creates_tables -v` → PASS (new migration applies on a fresh DB).
- [ ] **Step 3: Commit** — `git add socialhome/migrations/0021_app_kv.sql && git commit -m "feat: app_kv per-user storage table"` + `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## Task 2: Domain — `AppKvEntry` + `AppQuotaExceededError`

**Files:** Modify `socialhome/domain/apps.py`; Test: extend `tests/domain/test_apps.py`.

- [ ] **Step 1: Write failing tests** (append to `tests/domain/test_apps.py`)

```python
def test_app_kv_entry_is_frozen():
    from socialhome.domain.apps import AppKvEntry
    import dataclasses
    e = AppKvEntry(app_id="chess", user_id="u1", key="game:1",
                   value_json='{"turn":"w"}', updated_at="2026-06-02T00:00:00+00:00")
    assert e.key == "game:1"
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.key = "x"  # type: ignore[misc]


def test_app_quota_exceeded_is_app_error():
    from socialhome.domain.apps import AppQuotaExceededError, AppError
    assert issubclass(AppQuotaExceededError, AppError)
```

- [ ] **Step 2: Run** → FAIL (ImportError).
- [ ] **Step 3: Implement** — add to `socialhome/domain/apps.py`:

```python
class AppQuotaExceededError(AppError):
    """A per-(app, user) storage quota (key count / value size) was exceeded."""


@dataclass(slots=True, frozen=True)
class AppKvEntry:
    """One row of the per-user ``app_kv`` store."""

    app_id: str
    user_id: str
    key: str
    value_json: str
    updated_at: str
```

- [ ] **Step 4: Run** `pytest tests/domain/test_apps.py -v` → PASS.
- [ ] **Step 5: Commit** — `git add socialhome/domain/apps.py tests/domain/test_apps.py && git commit -m "feat: AppKvEntry + AppQuotaExceededError"` + trailer.

---

## Task 3: Repo — KV methods on `SqliteAppRepo`

**Files:** Modify `socialhome/repositories/app_repo.py`; Test: extend `tests/repositories/test_app_repo.py`.

**KV methods to add to `AbstractAppRepo` + `SqliteAppRepo`** (key + value are bound params):
- `kv_get(app_id, user_id, key) -> AppKvEntry | None`
- `kv_list(app_id, user_id) -> list[AppKvEntry]` (ordered by key)
- `kv_set(app_id, user_id, key, value_json, updated_at) -> None` — `INSERT ... ON CONFLICT(app_id,user_id,key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at`
- `kv_delete(app_id, user_id, key) -> None`
- `kv_count(app_id, user_id) -> int` (for quota: count of keys)

- [ ] **Step 1: Write failing tests** (append; reuse the `db` fixture + the `_app()` helper from PR1; an installed app row must exist first so the FK holds — install `_app()` then operate on KV. Use a real user? The `user_id` FK references `users(user_id)` with cascade. Check whether the test DB enforces FKs (it does — PR1 found `PRAGMA foreign_keys=ON`). So either seed a user row via the user repo/fixture, OR insert a minimal user. Look at how PR1's `test_app_repo.py` handled the `installed_by` FK (it used `None`); here `user_id` is NOT NULL, so you MUST seed a real user. Find the user-seeding helper/fixture used by other repo tests — `grep -rn "SqliteUserRepo\|create_user\|users" tests/repositories | head` — and seed one user id, then use it.)

```python
@pytest.mark.asyncio
async def test_kv_set_get_roundtrip(db, seeded_user):  # adapt fixture names
    repo = SqliteAppRepo(db)
    await repo.install(_app())               # satisfies app_id FK
    await repo.kv_set("chess", seeded_user, "game:1", '{"turn":"w"}',
                      "2026-06-02T00:00:00+00:00")
    got = await repo.kv_get("chess", seeded_user, "game:1")
    assert got is not None and got.value_json == '{"turn":"w"}'


@pytest.mark.asyncio
async def test_kv_set_upserts(db, seeded_user):
    repo = SqliteAppRepo(db)
    await repo.install(_app())
    await repo.kv_set("chess", seeded_user, "k", '"a"', "2026-06-02T00:00:00+00:00")
    await repo.kv_set("chess", seeded_user, "k", '"b"', "2026-06-02T00:01:00+00:00")
    assert (await repo.kv_get("chess", seeded_user, "k")).value_json == '"b"'
    assert await repo.kv_count("chess", seeded_user) == 1


@pytest.mark.asyncio
async def test_kv_list_and_delete(db, seeded_user):
    repo = SqliteAppRepo(db)
    await repo.install(_app())
    await repo.kv_set("chess", seeded_user, "a", "1", "2026-06-02T00:00:00+00:00")
    await repo.kv_set("chess", seeded_user, "b", "2", "2026-06-02T00:00:00+00:00")
    assert [e.key for e in await repo.kv_list("chess", seeded_user)] == ["a", "b"]
    await repo.kv_delete("chess", seeded_user, "a")
    assert {e.key for e in await repo.kv_list("chess", seeded_user)} == {"b"}


@pytest.mark.asyncio
async def test_kv_cascades_on_uninstall(db, seeded_user):
    repo = SqliteAppRepo(db)
    await repo.install(_app())
    await repo.kv_set("chess", seeded_user, "k", "1", "2026-06-02T00:00:00+00:00")
    await repo.uninstall("chess")
    assert await repo.kv_get("chess", seeded_user, "k") is None
    assert await repo.kv_count("chess", seeded_user) == 0
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the five methods on the Protocol + `SqliteAppRepo` (add `from ..domain.apps import AppKvEntry` and a `_row_to_kv(row)` helper). Use `self._db.fetchone/fetchall/enqueue`; all values bound `?`. Example for `kv_set`:

```python
async def kv_set(self, app_id, user_id, key, value_json, updated_at) -> None:
    await self._db.enqueue(
        """INSERT INTO app_kv(app_id, user_id, key, value_json, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(app_id, user_id, key)
           DO UPDATE SET value_json = excluded.value_json,
                         updated_at = excluded.updated_at""",
        (app_id, user_id, key, value_json, updated_at),
    )
```

- [ ] **Step 4: Run** `pytest tests/repositories/test_app_repo.py -v` → PASS.
- [ ] **Step 5: Commit** — `git add socialhome/repositories/app_repo.py tests/repositories/test_app_repo.py && git commit -m "feat: app_kv repo methods (get/set/list/delete/count)"` + trailer.

---

## Task 4: Service — KV API + quota on `AppService`

**Files:** Modify `socialhome/services/app_service.py`; Test: extend `tests/services/test_app_service.py`.

**Methods to add to `AppService`** (all enforce app installed + enabled, then scope by `user_id`):
- `store_get(app_id, user_id, key) -> object` — returns the parsed JSON value; raises `AppNotFoundError` if app missing, `AppNotEnabledError` if disabled, `KeyError`→ map to 404 OR return a sentinel. Decide: raise `AppNotFoundError`-style for a missing KEY? Cleaner: add a dedicated lookup that the route turns into 404. Return `None`-vs-404 ambiguity: store the fact of existence — have `store_get` raise `AppNotFoundError` only for the app, and return a `(found: bool, value)` or raise a `KeyError` for a missing key that the route maps to 404. **Chosen:** `store_get` returns the parsed value or raises `KeyError` for a missing key; the route catches `KeyError` → 404.
- `store_list(app_id, user_id) -> dict[str, object]` — `{key: parsed_value}` for all the caller's keys.
- `store_set(app_id, user_id, key, value) -> None` — validate `len(key) <= APP_KV_MAX_KEY_LEN`; `value_json = json.dumps(value)`; validate `len(value_json.encode()) <= APP_KV_MAX_VALUE_BYTES` else `AppQuotaExceededError`; if the key is NEW and `kv_count >= APP_KV_MAX_KEYS` → `AppQuotaExceededError`; `repo.kv_set(...)` with UTC `updated_at`.
- `store_delete(app_id, user_id, key) -> None`.
- A private `_require_enabled_app(app_id)` helper: `app = repo.get(app_id)`; `None` → `AppNotFoundError`; `not app.enabled` → `AppNotEnabledError`.

- [ ] **Step 1: Write failing tests** (use the fake repo from PR1's test, extended with the KV dict + `kv_count`; or use the real `db`-backed repo for an integration-style service test — prefer the existing fake-repo style for unit speed, adding kv storage to the fake). Cover:
  - `test_store_set_get_roundtrip` (value is a dict → round-trips parsed).
  - `test_store_set_requires_installed` → `AppNotFoundError`.
  - `test_store_set_requires_enabled` (install then disable) → `AppNotEnabledError`.
  - `test_store_get_missing_key_raises_keyerror`.
  - `test_store_value_too_large_raises_quota` (value_json > 64 KiB).
  - `test_store_too_many_keys_raises_quota` (monkeypatch `APP_KV_MAX_KEYS` to a small number, or set 2 and add a 3rd).
  - `test_store_list_returns_parsed_map`, `test_store_delete`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the methods + module-level quota constants. Reuse `_require_enabled_app`. `json.dumps`/`json.loads` for value encode/decode.
- [ ] **Step 4: Run** `pytest tests/services/test_app_service.py -v` → PASS.
- [ ] **Step 5: Commit** — `git add socialhome/services/app_service.py tests/services/test_app_service.py && git commit -m "feat: AppService per-user store get/set/list/delete + quota"` + trailer.

---

## Task 5: Routes — `/api/apps/{app_id}/store[/{key}]`

**Files:** Modify `socialhome/routes/apps.py`, `socialhome/routes/base.py`, `socialhome/routes/__init__.py`; Test: extend `tests/routes/test_apps.py`.

- [ ] **Step 1: Write failing integration tests** (reuse PR1's client/admin/member token fixtures; install an app first — either via a monkeypatched catalog install, or seed an `installed_apps` row directly through the repo on the app's DB; pick whichever PR1's tests already enable). Cover:
  - `test_store_put_then_get` (member PUTs `{"value": {...}}`, GETs it back).
  - `test_store_get_missing_key_404`.
  - `test_store_on_uninstalled_app_404`.
  - `test_store_list_scoped_to_caller` (user A's PUT not visible to user B's GET list).
  - `test_store_value_too_large_413`.
  - `test_store_delete`.
- [ ] **Step 2: Run** → FAIL (routes unregistered).
- [ ] **Step 3: Implement** two views in `routes/apps.py`:
  - `AppStoreCollectionView` — `get()` → `svc.store_list(app_id, self.user.user_id)` → `{"items": {key: value}}`.
  - `AppStoreItemView` — `get()` → `svc.store_get(...)` (KeyError → handled by base map to 404); `put()` → body `{"value": <any>}` (reject if `"value"` absent → 400), `svc.store_set(app_id, self.user.user_id, key, value)`, return `{"key", "value"}`; `delete()` → `svc.store_delete(...)`, `{"status":"ok"}`.
  - `app_id = self.match("app_id")`, `key = self.match("key")`. Never trust a client-sent user_id — always `self.user.user_id`.
  - Map new exceptions in `routes/base.py`: `AppQuotaExceededError` → 413 `QUOTA_EXCEEDED`; `AppNotEnabledError` → 403 `FORBIDDEN`; a bare `KeyError` from store_get is risky to map globally — instead catch it in the view's `get()` and return `error_response(404, "NOT_FOUND", "Key not found.")` (do NOT add a global `except KeyError` to base.py — too broad).
- [ ] **Step 4: Register** in `routes/__init__.py` (order: collection before item is fine; both are under the existing `/api/apps` prefix and don't collide with `{app_id}` since the literal `store` segment differentiates — but register the more specific `/api/apps/{app_id}/store/{key}` and `/api/apps/{app_id}/store` so they don't clash with `/api/apps/{app_id}`):
```python
    app.router.add_view("/api/apps/{app_id}/store", AppStoreCollectionView)
    app.router.add_view("/api/apps/{app_id}/store/{key}", AppStoreItemView)
```
- [ ] **Step 5: Run** `pytest tests/routes/test_apps.py -v` → PASS; then `pytest tests/routes/ -q` (base.py change regression).
- [ ] **Step 6: Commit** — `git add socialhome/routes/ tests/routes/test_apps.py && git commit -m "feat: per-user app store routes (/api/apps/{id}/store)"` + trailer.

---

## Task 6: Docs

**Files:** `docs/api.md`, `docs/database.md`.

- [ ] **Step 1:** `docs/api.md` — add the 4 store endpoints to the Apps table: `GET /api/apps/{app_id}/store`, `GET/PUT/DELETE /api/apps/{app_id}/store/{key}` (auth: any member; data is per-user; note 413 on quota).
- [ ] **Step 2:** `docs/database.md` — under the Apps heading, add the `app_kv` table (Purpose + columns + the two cascades), and remove the "PR2 will add app_kv" forward-note now that it exists. Add `0021_app_kv.sql` to the migrations list.
- [ ] **Step 3: Commit** — `git add docs/ && git commit -m "docs: app_kv table + per-user store endpoints"` + trailer.

---

## Final verification (PR2)
- [ ] `pytest tests/domain/test_apps.py tests/repositories/test_app_repo.py tests/services/test_app_service.py tests/routes/test_apps.py -q` — all PASS.
- [ ] `pytest tests/routes/ tests/db/ -q` — no regressions from base.py / migration.
- [ ] `ruff check socialhome && ruff format --check socialhome && mypy socialhome` — clean.
- [ ] PR labeled `feat`, based on `feat/social-home-apps-pr1` (stacked); body notes per-user scope, quota caps, and that the JS SDK (`sh.store.*`) + sandbox bridge land in PR3.

## Self-Review checklist
- **Coverage:** migration ✓(T1), domain ✓(T2), repo+cascade ✓(T3), service+quota+enabled-gate ✓(T4), routes+caller-scoping+413 ✓(T5), docs ✓(T6).
- **Type consistency:** `store_set(app_id, user_id, key, value)` / `store_get(...)→value` / `store_list(...)→dict` identical across T4↔T5; `kv_set(app_id,user_id,key,value_json,updated_at)` identical T3↔T4.
- **Security:** user_id always from `self.user`, never the body; key/value size caps before write; FK cascade tested.
