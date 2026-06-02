# Social Home Apps — Implementation Plan (PR3: Sandboxed Runtime)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Run an installed app's bundle inside a **sandboxed iframe** isolated from the bearer token and parent DOM, talking to the host SPA over a `postMessage` bridge that proxies the PR2 per-user store. This is the **§2 "no third-party trust" sign-off PR** — the sandbox is what makes executing fetched third-party JS tolerable.

**Architecture:** Backend serves bundle files from `media_path/apps/<app_id>/<version>/` via a new `AppBundleView`, authorized **without a bearer** (sandboxed opaque-origin iframes can't send `Authorization`). Auth = a **media-signer signature over the bundle *prefix*** `/api/apps/{app_id}/bundle/`, carried as `?exp&sig` on the entry URL and re-issued as a short-lived **path-scoped cookie** so relative sub-resource loads (which drop the query string) stay authorized. Every bundle response carries a strict **CSP** (`connect-src 'none'`) + `X-Frame-Options: SAMEORIGIN`. The SPA renders `<iframe sandbox="allow-scripts">` (no `allow-same-origin` → opaque origin) and a `postMessage` bridge — validated by `event.source === iframe.contentWindow` — proxies `store.*` to the PR2 routes. The app never receives the token.

**Tech Stack:** Python 3.14 / aiohttp; reuses `media_signer.MediaUrlSigner` + `auth.py` signed-URL machinery; Preact/TS SPA. Stacks on `feat/social-home-apps-pr2`.

> **Browser-verification caveat:** the live sandbox/CSP/postMessage behaviour can only be confirmed in a browser. If the dev server + Chrome are unavailable (as in the build sandbox), the **backend is fully integration-tested** and the **frontend is tsc + vitest-tested**, but the live iframe isolation MUST be verified by a reviewer locally — call this out in the PR, do not claim it.

---

## Auth design (the crux)
- `media_signer.MediaUrlSigner.sign(path, ttl=)` signs `"{path}|{exp}"`; `.verify(path, exp, sig)` checks it (constant-time, exp in the future). `auth.py`'s `SignedMediaStrategy` verifies `?exp&sig` against `request.path` for paths in `_SIGNED_PATH_PATTERNS`.
- **We sign the PREFIX** `"/api/apps/{app_id}/bundle/"`, not each file — so ONE (exp,sig) authorizes every file under that app's bundle for the TTL. The entry URL carries it as `?exp&sig`; the `AppBundleView` re-issues it as a cookie for sub-resources.
- Bundle paths bypass the global auth middleware (added to public-path patterns) and `AppBundleView` does its OWN auth (query sig OR cookie), returning 403 if neither is valid. This is safe: bundle bytes are non-secret admin-installed static code; the real boundary is the sandbox + the authenticated store API.
- The `/runtime` endpoint that MINTS the signed entry URL IS bearer-authed and checks the app is installed + enabled.

---

## File Structure (PR3)
- Create `socialhome/routes/app_bundle.py` — `AppRuntimeView` (`GET /api/apps/{app_id}/runtime`) + `AppBundleView` (`GET /api/apps/{app_id}/bundle/{tail:.*}`).
- Modify `socialhome/auth.py` — add bundle prefix to `_DEFAULT_PUBLIC_PATH_PATTERNS` so it bypasses bearer auth (the view self-authorizes).
- Modify `socialhome/routes/__init__.py` — register the two routes.
- Modify `socialhome/domain/apps.py` — harden `AppManifest.from_dict` to reject `..` path segments anywhere (not just prefix).
- Create `client/src/features/apps/bridge.ts` — host↔iframe postMessage RPC.
- Create `client/src/features/apps/AppHost.tsx` — sandboxed iframe + bridge mount + launch UI.
- Modify `client/src/features/apps/AppsPage.tsx` — "Open" button → launch AppHost.
- Modify `client/src/store/apps.ts` — `getRuntime(appId)` → `api.get('/api/apps/{id}/runtime')`.
- Tests: `tests/routes/test_app_bundle.py`, extend `tests/domain/test_apps.py`, `client/src/features/apps/bridge.test.ts`.
- Docs: `docs/api.md`, `docs/architecture.md`, `docs/principles.md`.

---

## Task 1: Harden `AppManifest.entry` against `..` segments

**Files:** Modify `socialhome/domain/apps.py`; extend `tests/domain/test_apps.py`.

PR2's final review flagged: `AppManifest.from_dict` only rejects `entry` that *starts with* `/` or `..`, so `"a/../../etc"` slips through. Now that `entry` is used to build a served path, reject `..` as any path segment and backslashes.

- [ ] **Step 1: failing tests** (append):
```python
def test_manifest_entry_rejects_dotdot_segment():
    from socialhome.domain.apps import AppManifest
    for bad in ("a/../b", "../x", "foo/../../etc", "a\\b"):
        with pytest.raises(ValueError):
            AppManifest.from_dict({"entry": bad, "capabilities": []})

def test_manifest_entry_accepts_nested_relative():
    from socialhome.domain.apps import AppManifest
    m = AppManifest.from_dict({"entry": "sub/index.html", "capabilities": []})
    assert m.entry == "sub/index.html"
```
- [ ] **Step 2: run** → the dotdot test FAILS.
- [ ] **Step 3: implement** — in `AppManifest.from_dict`, replace the `startswith(("/", ".."))` check with: reject if `entry` is absolute (`startswith("/")`), contains `"\\"`, or any segment of `entry.split("/")` equals `".."` or is empty. Keep the rest.
- [ ] **Step 4: run** `pytest tests/domain/test_apps.py -v` → PASS.
- [ ] **Step 5: commit** — `git add socialhome/domain/apps.py tests/domain/test_apps.py && git commit -m "fix: reject .. segments in app manifest entry"` + trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## Task 2: Backend — bundle serving + runtime endpoint (SECURITY-CRITICAL)

**Files:** Create `socialhome/routes/app_bundle.py`; modify `socialhome/auth.py`, `socialhome/routes/__init__.py`; Test: `tests/routes/test_app_bundle.py`.

### 2a. `auth.py` — let bundle paths bypass bearer auth
Add to `_DEFAULT_PUBLIC_PATH_PATTERNS` (the regex list) a pattern for the bundle subpath, e.g. `r"^/api/apps/[^/]+/bundle/"`. Read the existing list + comment first and match style. (The `/runtime` endpoint is NOT public — it stays bearer-authed.) This makes the global auth middleware skip bundle requests so `AppBundleView` can self-authorize via signature/cookie.

### 2b. `routes/app_bundle.py`
Constants: `BUNDLE_COOKIE_PREFIX = "sh_app_bundle_"`, `BUNDLE_TTL_SECONDS = 300`, a CSP string:
```python
APP_CSP = (
    "default-src 'none'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self' data:; media-src 'self' data:; "
    "connect-src 'none'; base-uri 'none'; form-action 'none'"
)
```

`AppRuntimeView(BaseView)` — `GET /api/apps/{app_id}/runtime` (bearer-authed; member):
- `app = await svc.get(app_id)`; if `None` → `error_response(404, "NOT_FOUND", ...)`; if `not app.enabled` → `error_response(403, "FORBIDDEN", "App is disabled.")`.
- `prefix = f"/api/apps/{app_id}/bundle/"`; `signer = self.request.app[media_signer_key]`; `signed = signer.sign(prefix, ttl=BUNDLE_TTL_SECONDS)` → returns `"{prefix}?exp=..&sig=.."`. Parse `exp`/`sig` out (or compute via signer internals — simplest: `from urllib.parse import urlsplit, parse_qs`; `q = parse_qs(urlsplit(signed).query)`).
- `entry_url = f"{prefix}{app.manifest.entry}?exp={exp}&sig={sig}"`.
- Return `self._json({"app_id": app_id, "name": app.name, "entry_url": entry_url, "self_user_id": self.user.user_id, "capabilities": list(app.manifest.capabilities)})`.

`AppBundleView(BaseView)` — `GET /api/apps/{app_id}/bundle/{tail:.*}` (self-authorizing):
- `app_id = self.match("app_id")`; `tail = self.match("tail")` (the relative path within the bundle, may be empty → serve manifest entry).
- `prefix = f"/api/apps/{app_id}/bundle/"`; `signer = self.request.app[media_signer_key]`.
- **Authorize**: a valid `?exp&sig` over `prefix` (`signer.verify(prefix, exp, sig)`) OR a valid cookie `f"{BUNDLE_COOKIE_PREFIX}{app_id}"` whose value is `"{exp}.{sig}"` and verifies over `prefix`. If neither → `error_response(403, "FORBIDDEN", "Unauthorized bundle access.")`.
- Look up the app (`svc.get(app_id)`) → 404 if missing; resolve the on-disk file: `base = Path(config.media_path) / app.bundle_path`; `rel = tail or app.manifest.entry`; reject if `rel` empty after fallback. **Path-traversal guard**: `target = (base / rel).resolve()`; if not `target.is_relative_to(base.resolve())` → `error_response(403, ...)`; if not `await aiofiles.os.path.isfile(target)` → 404.
- Build response: `web.FileResponse(target)` OR stream like `media.py`. Set headers: `Content-Security-Policy: APP_CSP`, `X-Frame-Options: SAMEORIGIN` (override the global DENY — set it explicitly on the response so the `setdefault` middleware keeps ours), `Cache-Control: private, max-age=60`, correct `Content-Type` (mimetypes).
- **If this request was authorized by `?exp&sig` (i.e. the entry load)**, also `response.set_cookie(f"{BUNDLE_COOKIE_PREFIX}{app_id}", f"{exp}.{sig}", max_age=BUNDLE_TTL_SECONDS, path=prefix, httponly=True, samesite="Lax")` so relative sub-resource loads carry it.

Reuse the `media.py` streaming + mimetypes idiom. Imports at top (`pathlib`, `mimetypes`, `aiofiles.os`, `media_signer_key`, `app_service_key`, `config_key`, `error_response`).

### 2c. Register in `routes/__init__.py`
```python
    app.router.add_view("/api/apps/{app_id}/runtime", AppRuntimeView)
    app.router.add_view("/api/apps/{app_id}/bundle/{tail:.*}", AppBundleView)
```
(Register AFTER the `/api/apps/{app_id}/store...` routes; the literal `runtime`/`bundle` segments differentiate. The `{tail:.*}` must allow empty + slashes.)

### 2d. Tests `tests/routes/test_app_bundle.py` (integration; install an enabled app with a real on-disk bundle dir — write `manifest.json` + `index.html` + `app.js` into `media_path/apps/<id>/<ver>/` and seed the `installed_apps` row via the repo, matching how `tests/routes/test_apps.py` seeds an installed app). Cover:
- `test_runtime_returns_signed_entry_url` — bearer GET `/runtime` → 200, `entry_url` contains `exp=` and `sig=` and ends with the manifest entry path.
- `test_runtime_404_uninstalled`, `test_runtime_403_disabled`.
- `test_bundle_entry_served_with_valid_sig` — GET the `entry_url` (NO bearer) → 200, body is the index.html, response has the CSP header + `X-Frame-Options: SAMEORIGIN` + a `Set-Cookie` for the bundle.
- `test_bundle_rejects_missing_sig` — GET a bundle path with no sig + no cookie → 403.
- `test_bundle_subresource_via_cookie` — extract the Set-Cookie from the entry response, GET `app.js` WITH that cookie but NO query sig → 200 (proves sub-resources work).
- `test_bundle_path_traversal_blocked` — GET `/api/apps/{id}/bundle/../../../etc/passwd?exp&sig` (valid sig) → 403/404, no escape.
- `test_bundle_expired_sig_rejected` — a sig with `exp` in the past → 403 (use the signer with `now=` if needed, or a hand-built expired pair).
- [ ] TDD: write tests → FAIL → implement 2a/2b/2c → PASS.
- [ ] `pytest tests/routes/test_app_bundle.py -v` → PASS; `pytest tests/routes/ -q` no regression.
- [ ] `ruff check` + `ruff format` + `mypy` changed files.
- [ ] commit — `git add socialhome/routes/app_bundle.py socialhome/auth.py socialhome/routes/__init__.py tests/routes/test_app_bundle.py && git commit -m "feat: sandboxed app bundle serving + runtime endpoint (signed prefix + cookie, CSP)"` + trailer.

---

## Task 3: SPA bridge — `bridge.ts`

**Files:** Create `client/src/features/apps/bridge.ts`; Test: `client/src/features/apps/bridge.test.ts`.

A host-side controller that, given an `appId` + the iframe element, listens for `message` events and answers a small RPC protocol. The app posts `{id, method, params}`; host validates + replies `{id, ok, result|error}`.

- **Security:** ignore any `message` whose `event.source !== iframe.contentWindow` (the opaque-origin iframe's `event.origin` is `"null"`, so source-identity is the check, NOT origin). Never pass the bearer token into the iframe.
- **Methods** (proxy to PR2 routes via the `api` singleton):
  - `store.get(key)` → `api.get('/api/apps/{appId}/store/{key}')` → returns `.value` (catch 404 → return `null`/`undefined`).
  - `store.set(key, value)` → `api.put('/api/apps/{appId}/store/{key}', {value})`.
  - `store.delete(key)` → `api.delete(...)`.
  - `store.list()` → `api.get('/api/apps/{appId}/store')` → `.items`.
  - `app.context()` → `{appId, selfUserId}` (from the runtime payload the host already has; no token).
  - unknown method → reply `{ok:false, error:"unknown method"}`.
- **Real-time channel (WebSocket relay):** the iframe must NOT open its own WS (no token). Instead the host owns the authenticated SPA WS (`client/src/ws.ts` — `ws.on(type, handler) => unsubscribe`, frames are `{type, data}`) and the bridge **relays app-scoped frames into the iframe**. In `mountBridge`, subscribe `ws.on('app.message', evt => { if (evt.data.app_id === appId) iframe.contentWindow.postMessage({type:'app:event', payload: evt.data.payload}, '*') })`. The app SDK exposes `sh.onMessage(cb)`. This is the same delivery path PR4 wires federation into (inbound cross-household app messages → `app.message` WS frame). Outbound app→peer send is PR4 (`app.send` will proxy to a federation route); in PR3 `app.send` may reply `{ok:false, error:"federation not available"}` so the SDK shape is stable.
- Export `mountBridge(iframe: HTMLIFrameElement, ctx: {appId, selfUserId}) => () => void` — returns a cleanup that removes BOTH the `message` listener AND the `ws.on` subscription.

**Backend WS frame contract (define now, produce in PR4):** document `app.message` as a known outbound WS frame `{type:"app.message", app_id, payload}` in `docs/api.md`'s WebSocket frame list. PR3 ships no producer (the bridge relay is unit-tested with a simulated frame); PR4's federation inbound handler is the first producer. Add a vitest case asserting the relay forwards a matching-`app_id` frame to the iframe and ignores a non-matching one.

- [ ] **Step 1: vitest tests** `bridge.test.ts` — mock `api`; simulate `window.postMessage`-style events by calling the registered handler with a fake event `{source: <fake contentWindow>, data: {...}}`; assert: a message from the wrong `source` is ignored; `store.set` calls `api.put` with the right path+body; `store.get` returns `.value`; unknown method → error reply; the reply is posted back to the iframe `contentWindow.postMessage`. (Construct the iframe stub with a `contentWindow` whose `postMessage` is a vi.fn.) Follow an existing `client/src/**/*.test.ts` for the mock-`api` style.
- [ ] **Step 2: run** → FAIL. **Step 3: implement** `bridge.ts`. **Step 4:** `pnpm vitest run src/features/apps/bridge.test.ts` → PASS; `pnpm tsc --noEmit`.
- [ ] **Step 5: commit** — `git add client/src/features/apps/bridge.ts client/src/features/apps/bridge.test.ts && git commit -m "feat: app host postMessage bridge (store proxy, source-validated)"` + trailer.

---

## Task 4: SPA — `AppHost` iframe + launch UI

**Files:** Create `client/src/features/apps/AppHost.tsx`; modify `client/src/features/apps/AppsPage.tsx`, `client/src/store/apps.ts`.

- `store/apps.ts`: add `getRuntime(appId)` → `api.get('/api/apps/{appId}/runtime')` returning `{entry_url, self_user_id, ...}`.
- `AppHost.tsx`: given `appId`, on mount calls `getRuntime`, then renders `<iframe sandbox="allow-scripts" src={addBase(entry_url)} class="sh-app-frame" title={name}>`. **Must use `addBase` (baseUrl helper) so the ingress prefix is honoured.** Mount the bridge (`mountBridge(iframeRef, {appId, selfUserId})`) once the iframe ref is set; clean up on unmount. Show a loading state while `getRuntime` resolves and an error state on failure. A close/back control returns to the apps list.
- `AppsPage.tsx`: add an "Open" `Button` to each enabled app card (all members, not just admins) that launches the app — either route to `/apps/{appId}` (add the route) rendering `AppHost`, or a full-screen overlay. Prefer a route `'/apps/:appId'` in `router.ts` if the router supports params (check how other param routes are declared), else an in-page overlay state. Keep the existing install/enable/uninstall controls.
- **CRITICAL frontend rules:** `sandbox="allow-scripts"` ONLY (NEVER add `allow-same-origin` — that would give the app the parent origin + token access). iframe `src` via `addBase`. Bridge validates `event.source`.

- [ ] **Step 1:** implement `getRuntime` + `AppHost` + the Open button + route/overlay.
- [ ] **Step 2:** `cd client && pnpm tsc --noEmit && pnpm vitest run && pnpm build` → all green (update `router.test.ts` route count if you add a route).
- [ ] **Step 3 (visual verification):** attempt `pnpm dev` + backend + chrome-devtools MCP to confirm the iframe renders sandboxed at desktop + mobile and the app can read/write its store via the bridge. **If unavailable, clearly report "live sandbox verification not performed (no dev server/Chrome)"** and describe what a reviewer must check: iframe has `sandbox="allow-scripts"` and NO `allow-same-origin`; CSP blocks `connect-src`; bridge rejects wrong-source messages; store round-trips.
- [ ] **Step 4: commit** — `git add client/src/features/apps client/src/store/apps.ts client/src/router.ts client/src/router.test.ts && git commit -m "feat: AppHost sandboxed iframe + Open launch"` + trailer.

---

## Task 5: Docs + principle sign-off update

**Files:** `docs/api.md`, `docs/architecture.md`, `docs/principles.md`.

- [ ] `docs/api.md` — add `GET /api/apps/{app_id}/runtime` (member; mints signed entry URL) and `GET /api/apps/{app_id}/bundle/{tail}` (signed-URL/cookie, serves bundle files with CSP) to the Apps table.
- [ ] `docs/architecture.md` — expand the Social Home Apps section with the sandbox runtime: signed-prefix + cookie bundle auth, the CSP/`X-Frame-Options` posture, the `sandbox="allow-scripts"` opaque-origin iframe, and the postMessage bridge that withholds the token. Forward pointer: PR4 federation channel.
- [ ] `docs/principles.md` — update the §2 sign-off note: the sandbox runtime that gates "execute fetched third-party JS" has now shipped (describe the three layers: sha256-verified bundle, opaque-origin sandbox + `connect-src 'none'`, token-withholding bridge).
- [ ] commit — `git add docs/ && git commit -m "docs: app sandbox runtime (api, architecture, principles sign-off)"` + trailer.

---

## Final verification (PR3)
- [ ] `pytest tests/domain/test_apps.py tests/routes/test_app_bundle.py tests/routes/test_apps.py -q` — PASS.
- [ ] `pytest tests/routes/ tests/db/ -q` — no regression (auth.py public-pattern change is the risk).
- [ ] `ruff check socialhome && mypy socialhome`; `cd client && pnpm tsc --noEmit && pnpm vitest run && pnpm build`.
- [ ] Dispatch a final whole-PR reviewer (focus: bundle auth can't be bypassed; CSP/sandbox correctness; no token leak; path-traversal; `X-Frame-Options` override actually wins over the global DENY).
- [ ] PR labeled `feat` + `security`, based on `feat/social-home-apps-pr2`; body documents the signed-prefix+cookie scheme, the CSP, and the **browser-verification caveat**.

## Self-Review checklist
- **Coverage:** manifest hardening ✓(T1), bundle serving+auth+CSP+runtime ✓(T2), bridge ✓(T3), iframe+launch ✓(T4), docs+sign-off ✓(T5).
- **Security:** bundle 403 without sig/cookie; prefix sig (not per-file); cookie path-scoped + HttpOnly + short TTL; `sandbox` has NO `allow-same-origin`; bridge validates `event.source`; token never enters the iframe; `connect-src 'none'`.
- **Type/route consistency:** `entry_url` shape from `/runtime` matches what `AppHost` consumes; `{tail:.*}` allows empty + nested paths; `X-Frame-Options` set on the response (not setdefault) so it overrides the global DENY.
