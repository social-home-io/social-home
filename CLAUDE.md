# CLAUDE.md — socialhome

Canonical AI-agent instruction file for the Social Home core application.
Also visible as `AGENTS.md`, `.cursorrules`, and
`.github/copilot-instructions.md` — those are symlinks to this file so
the three agent surfaces can't drift apart.

Read spec_work.md (the project specification) before making any changes.
The spec is the source of truth — if code and spec disagree, fix the code.

### Architecture

- **Platform adapters:** three modes selected by `SH_MODE`:
  `standalone` → `StandaloneAdapter` (local users, password auth),
  `ha` → `HaAdapter` (HA Core REST + local password auth),
  `haos` → `HaosAdapter` (HA Supervisor add-on with Ingress).
  ALL adapter-specific code lives in `socialhome/platform/`. Never branch
  on `config.mode` outside `platform/` and `config.py` — query
  `adapter.capabilities` instead. Never `isinstance`-check concrete
  adapters — use Provider methods (`adapter.auth.authenticate`,
  `adapter.users.list_users`, etc.). Adapter lifecycle hooks
  (`on_startup`, `on_cleanup`, `get_extra_services`, `get_extra_routes`)
  handle mode-specific wiring. Platform events for HA automations are
  defined in `HaBridgeService` — add new event types there, not in
  `app.py`.
- **Service layer:** all business logic lives in `socialhome/services/`.
  Route handlers in `routes/` are thin `BaseView` subclasses — one view class
  per REST resource, dispatched by HTTP method (`get()`, `post()`, `patch()`,
  `delete()`). Use `self.svc(key)` for service access, `self.user` for auth,
  `self._json(data)` for responses. Domain exceptions are mapped centrally
  by `BaseView._iter()` — individual handlers do NOT need try/except blocks.
  See `routes/base.py` for the base class.
  No SQL in route handlers. No business logic in repositories.
- **Repository pattern:** `repositories/` contains abstract base classes and
  SQLite implementations. Services depend on `Abstract*Repo` Protocols only —
  never on concrete `Sqlite*Repo` implementations. The two whole-table-dump
  services that need raw SQL (`backup_service`, `data_export_service`) are
  the documented exception.
- **No SQL outside `repositories/`:** services and routes call repo methods,
  not `db.fetchall` / `db.enqueue` / `db.transact` directly. Adding new SQL to
  a service is a smell — extract the query into the appropriate repo.
- **Domain layer:** `domain/` contains pure dataclasses — no I/O, no imports from
  services or repos. Keep them frozen (`@dataclass(slots=True, frozen=True)`).
  Row-shaped dataclasses live in `domain/`, not `repositories/`. The only
  exception is repo-local DTOs (e.g. `SearchHit`) that never escape the repo
  layer; document them with "repo-local DTO" in the class docstring.
- **`create_app` stays thin:** new wiring goes into the matching `_build_*`
  factory in `app.py` (`_build_repos`, `_build_middleware`). The `create_app`
  body should only orchestrate, not enumerate.
- **Async everywhere:** all I/O is `async def`. Never use `time.sleep()` —
  use `asyncio.sleep()`. Never call blocking I/O without `run_in_executor`.
- **Filesystem I/O goes through `aiofiles`** — never `open(...)`,
  `Path.read_bytes/write_bytes/read_text/write_text`, `Path.is_file/exists/stat`,
  `Path.mkdir/unlink/replace`, or `shutil.*` inside an `async def`. The
  async equivalents live in `aiofiles` (`aiofiles.open`) and `aiofiles.os`
  (`aiofiles.os.path.isfile`, `.stat`, `.makedirs`, `.remove`, `.replace`).
  Reference call sites: `routes/media.py`, `services/dm_media_sync_service.py`,
  `services/federation_inbound_service.py` (DM media blob assembly),
  `services/highlight_signaling_handler.py` (DataChannel streaming).
- **CPU-bound work goes through `asyncio.to_thread`** — Pillow
  decode/encode, QR PNG rendering, gzip / tarfile assembly, hashlib over
  large buffers, anything that would burn ≥ tens of ms of CPU. Pattern is
  `return await asyncio.to_thread(self._sync_helper, *args)` with the
  blocking body extracted into a sibling sync method. Reference call
  sites: `media/image_processor.py` (`process` / `generate_thumbnail`),
  `media/video_processor.py` (PyAV transcode), `media/audio_processor.py`
  (PyAV probe), `services/audio_transcription_service.py` (Opus → PCM16
  decode), `services/backup_service.py` (`_build_tarball_bytes`,
  `_read_restore_tar`), `global_server/public.py`
  (`_render_qr_png_data_uri`), `services/gallery_service.py`
  (`_extract_exif_date_sync`, `_read_dims_sync`).
- **Schedulers follow the `asyncio.Event` lifecycle.** Every background loop
  takes the shape: `_stop: asyncio.Event` set in `stop()`, drained in `start()`,
  body is `while not self._stop.is_set()`. Reference template:
  `infrastructure/replay_cache_scheduler.py`. Do not introduce a `_running:
  bool` flag instead — that pattern is gone from the codebase. Cluster /
  heartbeat / discovery loops follow the same shape (see
  `global_server/cluster.py:_heartbeat_loop`,
  `services/public_space_discovery_service.py:_poll_loop`) — `cancel()`
  alone is not a substitute, because it can rip the task out mid-DB-write
  or mid-HTTP-request. The sole exception is a queue-driven worker that
  uses a sentinel value (e.g. `db/database.py:_writer_loop`); document it
  so a future reader doesn't "fix" it.

### Design patterns

- **Registry** for federation event dispatch. New inbound event handlers
  register via `_event_registry.register(event_type, handler)` in the
  service's `attach_*` method. Never add if/elif branches to
  `_dispatch_event`. Reference: `federation/event_dispatch_registry.py`.
- **Middleware chain** for the §24.11 inbound federation validation pipeline.
  Each validation step (JSON parse, instance lookup, timestamp check, signature
  verify, replay check, decrypt, idempotency, ban check, persist replay) is an
  independently-testable async callable composed via `InboundPipeline`. New
  steps (quota enforcement, sealed-sender unseal) are added by appending to
  the chain — never by editing the monolithic `handle_inbound_envelope`. The
  same chain validates both HTTPS-inbox and DataChannel-delivered envelopes.
  Reference: `federation/inbound_validator.py`.
- **Strategy** for transport + envelope crypto. Concrete classes
  (`HttpsInboxTransport`, `_RtcPeer`, `FederationEncoder`) satisfy the
  `TransportStrategy` / `EncryptionStrategy` Protocols in
  `federation/strategies.py`. New transports / crypto schemes plug in by
  satisfying the protocol — no `FederationService` edits required.
- **Specification** for repo list/search reads with arbitrary filter
  combinations. Build a `Spec(where=[...], order_by=[...], limit=N)` and
  call `repo.find(spec)`. The repo's `_*_COLS` allow-list defends the
  column names against injection. Keep bespoke `list_*` methods for the
  common cases — Spec is the escape hatch, not a wholesale replacement.
  Reference: `repositories/_spec.py`, used by `notification_repo` and
  `post_repo`.
- **Unit of Work** for any handler that needs more than one write and at
  least one domain event to ship together. `async with UnitOfWork(db,
  bus=bus) as uow` opens a `BEGIN IMMEDIATE`, buffers `uow.exec(...)`
  writes, and dispatches `uow.publish(...)` events only after commit —
  so a handler crash never publishes events whose writes rolled back.
  See `db/unit_of_work.py`. Single-write handlers can keep using
  `db.enqueue` directly.
- **Adapter** for platform integration. The `PlatformAdapter` ABC in
  `platform/adapter.py` composes small `*Provider` Protocols
  (`AuthProvider`, `UserDirectory`, `PushProvider`, `STTProvider`,
  `AIProvider`, `ExternalEventSink`) plus a `capabilities` set
  (`Capability.PUSH | STT | AI | INGRESS | PASSWORD_AUTH |
  HA_PERSON_DIRECTORY`). Concrete adapters
  (`StandaloneAdapter`, `HaAdapter`, `HaosAdapter`) wire mode-specific
  providers in their constructor and never branch on `config.mode`
  themselves. Add a new platform by writing a new adapter class plus
  the providers it needs; route handlers and services consume the
  adapter through the Provider interfaces. Reference:
  `platform/adapter.py`, `platform/{standalone,ha,haos}/`,
  `platform/local_credentials.py` (shared password+token machinery
  composed by adapters that opt into `PASSWORD_AUTH`).

### Code Conventions

- Python 3.14+. Use `match/case` for event dispatch.
- Type hints on all public methods. Use `str | None` not `Optional[str]`.
- `log = logging.getLogger(__name__)` at module level — never `print()`.
- **All imports at the top of the file** — never import inside a function
  or method. The only exception is `if TYPE_CHECKING:` blocks for type
  annotations that would otherwise cause circular imports. If a top-level
  import causes a circular dependency, restructure the modules or use
  `TYPE_CHECKING`.
- SQLite: always use `AsyncDatabase.enqueue()` for writes (WAL coalescing).
  Use `fetchall()` / `fetchone()` for reads.
- Error handling: raise domain exceptions (`SpaceNotFoundError`, etc.) in services.
  Route handlers catch them with `_map_exc()`.
- Never add `*Addendum`, `*Extension`, or `*Complete` subclasses.
  Merge changes directly into the existing class.
- **Prefer mixins for cross-cutting behaviour.** When the same
  attribute + method pair recurs across multiple unrelated services
  (presence, DMs, highlights, moments all need to filter on the
  per-pair visibility hide list — that kind of "recurs"), pull it
  into a small mixin class and inherit from it. One implementation,
  one slot, one test surface. Pattern: the mixin owns the slot via
  ``__slots__ = ("_field",)``; the subclass writes to that slot in
  its own ``__init__`` and inherits the method that reads it. The
  canonical example is
  :class:`socialhome.services.visibility.VisibilityMixin` (consumed
  by ~10 outbound services). Don't reach for inheritance to bolt
  *one* new field onto an unrelated class — that's the "patch gap"
  anti-pattern the `*Addendum` rule above already covers; the
  mixin pattern is for behaviour that genuinely recurs, not for
  threading a one-off dependency.
- **Module filenames never start with a leading underscore.** A new
  shared helper lives at `socialhome/services/visibility.py`, not
  `socialhome/services/_visibility.py`; the matching test is
  `tests/services/test_visibility.py`. Privacy is signalled by package
  boundaries and what the package's `__init__.py` re-exports, not by
  the filename. The same applies to repo-local DTOs and any other
  internal module — pick a plain name. Existing `_`-prefixed modules
  (e.g. `repositories/_spec.py`) are grandfathered; do not add new
  ones, and rename when you touch them for unrelated reasons only if
  the rename is mechanical.

### Frontend (SPA) & ingress

The Preact SPA in `client/` runs behind three different document bases
depending on the platform mode:

- **standalone / dev** — document base is `/`. `fetch('/api/me')`
  resolves against the document origin and reaches the backend.
- **ha** — same shape as standalone when reachable directly, OR
  served behind a reverse proxy with a path prefix.
- **haos** — document base is HA Supervisor's
  `/api/hassio_ingress/<token>/`. The Supervisor proxies every
  request to the add-on container, ADDING the auth headers
  (`X-Hass-Source: core.ingress`, `X-Remote-User-Name`) that the
  backend's `HaIngressStrategy` accepts as the handshake. The SPA
  carries NO bearer token in this mode.

All three deployments load the same `index.html` and the same JS
bundle. The backend's `SpaIndexView` rewrites `<base href>` from the
Supervisor-injected `X-Ingress-Path` header per request; the SPA
reads `document.baseURI` once at module load and anchors every
runtime URL on it.

The rules that keep this working:

- **Every URL in the SPA is anchored on `document.baseURI`**, never
  on the document origin. Use the helpers in `client/src/baseUrl.ts`:
  - For API fetches: call `api.get/post/patch/delete`; the api client
    strips the leading slash so `fetch` resolves the path against
    `<base href>`. Never call `fetch('/api/...')` directly. Same for
    WebSockets — the `ws` manager passes `'api/ws'` (no leading slash).
  - For `window.location.href` / `.assign` to the app root: use
    `basePath`. Hard-coding `'/'` skips the ingress prefix and
    bounces the iframe to HA Core's frontend.
  - For `window.location.href` to an in-app path: wrap in
    `addBase('/spaces/...')` so the ingress prefix is prepended.
  - For markdown / user-supplied links: see `utils/markdown.ts` — it
    already routes through the base; don't reinvent.
- **Auth state is mode-agnostic**: `isAuthed` is `currentUser != null`.
  A successful `/api/me` is the only signal of a working auth
  handshake — that holds for bearer (standalone/ha) and ingress
  (haos) alike. Don't reintroduce a `token != null` requirement in
  the gate.
- **In haos mode the SPA never has a token.** `POST
  /api/setup/haos/complete` deliberately returns `{username}` and no
  token — ingress is the only entry point. Cold-start probes
  `/api/me` without an `Authorization` header and lets the
  Supervisor-added ingress headers do the auth. A new setup
  endpoint for haos must follow the same pattern (no token, no
  password collection); standalone / ha setup endpoints DO return a
  token.
- **A 401 without a stashed token is NOT a session expiry.** The
  api client guards the "Session expired" toast on `token.value !==
  null` — never show the toast for an ingress probe that 401'd.
  The App shell renders a dedicated `IngressAuthFailed` page in that
  case (the right surface for "Supervisor headers aren't reaching
  us" is a reload-the-sidebar-entry CTA, not a password form).
- **Tests cover the ingress shape.** `client/src/baseUrl.test.ts`
  exercises the path-rewriting helpers and
  `IngressLocationProvider.test.tsx` exercises the router. When
  you add a new URL surface (a redirect, a fetch target, an icon /
  manifest reference), extend these — the test names mention the
  ingress prefix explicitly so a future contributor sees the
  invariant before they write code that breaks it.

References:
- `client/src/baseUrl.ts` — `basePath`, `addBase`, `stripBase`
- `client/src/api.ts` — `_rel` strips leading slashes
- `client/src/ws.ts` — relative WS path resolves against
  `document.baseURI`
- `client/src/router/IngressLocationProvider.tsx` — preact-iso
  router glue
- `socialhome/routes/spa.py` — `SpaIndexView` rewrites `<base href>`
  from `X-Ingress-Path`
- `socialhome/auth.py` — `HaIngressStrategy` is the backend
  authentication strategy that accepts the ingress headers

### Visually verifying UI changes

**Every change that touches the SPA — layout, components, CSS,
copy, dialog flow, form interactions, anything a user can see or
click — MUST be both (a) visually verified in Chrome on desktop
*and* mobile viewports AND (b) walked through as a UX review pass
before the PR is opened.** This is not "if the change feels
visual"; it is every change that mounts a component, mutates the
DOM, or alters user-facing copy. Type-checking and Vitest cover
code correctness, not visual or UX correctness — a button can pass
`tsc` + tests and still bleed off-screen on a 360 px phone, stack
vertically on desktop, leave a date input stranded in the past,
or make the user fight a default they'd never have picked.

Use the `chrome-devtools-mcp:chrome-devtools` skill against a
local `pnpm dev` boot. `resize_page` flips between sizes; reuse
the same browser tab so you compare the same state.

**Both viewports are mandatory:**

- **Desktop** at ~1280×720 — confirm the layout reads as designed,
  nothing overflows the column, hover states do what they say,
  focus rings land where keyboard users expect them.
- **Mobile** at ~390×844 (iPhone 14 / Pixel-class) — confirm the
  strip / chip / form wraps correctly, tap targets stay ≥ 36 px
  tall, horizontal scroll doesn't appear on body content, text
  doesn't get clipped, sticky chrome doesn't cover the active
  control.

**The UX review pass is mandatory too.** Visual verification
proves "the pixels rendered." UX review proves "a real person
trying to use this gets through it cleanly." Before opening the
PR, walk the touched surface yourself with the user's goal in
mind and ask:

- **Defaults:** is the pre-filled state what the user actually
  wants 90% of the time? (e.g. when a start date moves past the
  end date, does the end follow automatically?)
- **Error / empty states:** what does the user see when the
  network fails, the list is empty, the input is invalid? Is the
  next action obvious from that state alone?
- **Discoverability:** can a first-time user find the new
  control without being told it's there? Is the affordance
  (button vs link vs chip) honest about what happens on click?
- **Recovery:** if the user makes the wrong choice, how do they
  back out? Is "undo" or "edit" reachable in one click?
- **Cross-state coherence:** does the change look right when the
  surrounding state is *different* (no events, one event, many
  events; mobile keyboard up; long copy in another language;
  RTL)?
- **Keyboard / a11y:** can a keyboard-only or screen-reader user
  reach and operate the new control? Do focus rings render
  somewhere visible against the surface tint?

Take screenshots of both viewports + a short note (in the PR
description) on what the UX review surfaced and how you handled
it. "No issues" is a valid outcome but you must have actually
looked. If the dev server can't be reached (sandbox, offline),
say so explicitly in the PR description rather than claiming
verification — the missing screenshots / review notes are
themselves the signal that a reviewer should reproduce locally
before merging.

### Federation & Security

- Every inbound federation event MUST pass through the §24.11 validation pipeline:
  JSON parse → timestamp ±300 s → instance lookup → ban check → Ed25519 verify
  → replay cache → dispatch.
- Never skip signature verification for "trusted" instances.
- GPS coordinates: truncate to 4 decimal places before any storage or transmission.
- WebSocket auth uses `?token=` query parameter (unavoidable for browser WS).
  Raw API tokens appear in access logs and browser history — operators must
  redact tokens from logs. Never log the full query string of `/api/ws`.
  `round(float(lat), 4)` — never store raw precision from device.
- Push notification payloads: title only, body omitted for DMs, location messages,
  and any user-generated content (§25.3).
- `SENSITIVE_FIELDS` frozenset in `security.py` — never expose these in API responses.

### Testing

- Unit tests: no real network, no real disk. All repos are in-memory stubs.
- Integration tests: real SQLite in `tmp_path`, real aiohttp `TestClient`.
- **Every Python module under `socialhome/` has a same-named test file** under
  `tests/<matching subpath>/test_<module>.py` — `socialhome/services/foo.py`
  → `tests/services/test_foo.py`, `socialhome/repositories/bar_repo.py` →
  `tests/repositories/test_bar_repo.py`, `socialhome/routes/baz.py` →
  `tests/routes/test_baz.py`. The test file is added in the same commit as
  the production file. The only exception is the `__init__.py` re-export
  shims, which carry no logic.
- Protocol tests in `tests/protocol/` are a **release blocker** — never skip them.
- Run `pytest tests/protocol/ -m security` before every commit touching federation
  or presence code.
- Coverage gate: 90% branch coverage. `pytest --cov=socialhome --cov-fail-under=90`.

### Pre-commit hooks

- Config: `.pre-commit-config.yaml`. Install once per clone with
  `pip install pre-commit && pre-commit install`.
- Hooks: ruff (lint + format), mypy (on `socialhome/`), frontend
  ESLint + `tsc --noEmit` on staged TS/TSX, and `pnpm build` at
  pre-push time.
- If a hook fails, fix the underlying issue — never pass `--no-verify`.

### Releases

- CalVer (`YYYY.M.D`). The **Release — draft** workflow
  (`.github/workflows/release-draft.yml`) maintains a draft
  release that always reflects the current merge backlog. It
  fires automatically on **every push to `main`** (every PR
  merge) AND on manual `workflow_dispatch` (Actions → *Release —
  draft* → *Run workflow* — the manual path also takes an
  optional `date` input for back-dating). Each run computes the
  next `YYYY.M.D[.N]` tag (bumping `.N` when a same-day release
  is already published), asks GitHub to auto-generate the notes
  from merged PRs since the last published release, and creates
  or refreshes a **draft** release. If a draft for the same
  version already exists (an earlier merge today), the workflow
  deletes and recreates it so the notes always reflect the
  current merged set — un-published drafts are by contract the
  auto-generated view, not a place to keep hand-polish.
  Publishing the draft from the UI is what creates the tag and
  fires `release.published`. Cutting by hand still works —
  `gh release create 2026.5.12 --target main --title 2026.5.12 --notes …`
  — for one-off / out-of-band releases.
- `.github/workflows/publish.yml` listens for `release.published`
  and runs three jobs in parallel: PyPI (via OIDC trusted
  publishing), core Docker image to
  `ghcr.io/social-home-io/socialhome`, and GFS Docker image to
  `ghcr.io/social-home-io/gfs`. Each Docker push gets four tags:
  pinned (`:2026.5.10`), month track (`:2026.5`), year track
  (`:2026`), and `:latest`.
- Bare `git tag && git push --tags` does NOT publish anymore —
  the publish workflow only listens for `release.published` so
  that operators always go through the release UI / CLI (and so
  the pipeline never runs twice for the same release).
- The version is dynamic via `hatch-vcs` — do NOT edit the
  version field in `pyproject.toml`. The tag IS the version.
  No `v` prefix.

### PR labels (drive the changelog)

The auto-generated release notes group PRs by label
(`.github/release.yml`), so **every PR needs at least one of**:

| Label | Bucket | When to use |
|---|---|---|
| `breaking` | Breaking changes | API / config / schema change that needs migration on the user's side |
| `feat` | Features | New user-visible capability |
| `fix` | Fixes | Bug fix, regression repair, edge-case hardening |
| `security` | Security | Vulnerability fix, hardening pass on auth / crypto / federation |
| `docs` | Docs | Docs-only change (the matching-doc-update rule above) |
| `chore` | Other changes | Tooling, devcontainer, CI, dependency bump, refactor with no behaviour change |

The label drives **which bucket** the PR lands in; the PR title
is what shows up underneath. So a tight, user-facing title beats a
mechanical conventional-commit prefix — write *"Pictures are
click-to-zoom in the gallery again"*, not *"fix(spa): mount the
ImageLightbox overlay"*.

Two automatic exclusions: PRs from `dependabot` and PRs tagged
`skip-changelog` or `dependencies` are dropped from the release
notes entirely. Use `skip-changelog` for trivial back-end cleanup
that has no user-visible effect.

When opening a PR with `gh pr create`, attach the label with
`--label fix` (or `feat`, `chore`, …). The reviewer should bounce
a PR that's missing a category label — an unlabelled PR still
lands in *Other changes* via the catch-all, but the bucket loses
its meaning fast if "Other" becomes the default.

### Encryption-First Rule (§25.8.21)

Every field in every outgoing federation event is encrypted unless the
federation service needs it in plaintext to route or validate the event.
When adding a new federation event:
- Put routing fields (event_type, from/to instance, space_id, epoch) in plaintext
- Put everything else — content, names, counts, choices — inside the encrypted payload
- Never add a `"payload": plaintext_fallback` pattern
- If `SpaceContentEncryption` is not configured, raise `RuntimeError` — never degrade silently

**Hard rule — non-member households MUST NOT see space content.** A
household that isn't a member of a space, but happens to be on the
federation mesh between the host and a remote member, is acting as a
routing relay. It MUST NOT be able to read any space content (posts,
comments, reactions, calendar events, location pins, …). This means:

- **Direct delivery: target members only.** Use
  `broadcast_to_space_members` for every space-content fan-out —
  it targets `space_instances` (households with at least one member),
  not arbitrary paired peers. Sending a `SPACE_*` event to a non-member
  household is a bug, not an optimization.
- **Mesh-routed delivery: end-to-end sealed.** When a path crosses a
  non-member relay, wrap with `SPACE_ROUTED` so the relay sees only
  opaque ciphertext sealed under the target's ephemeral X25519 pub
  (see `socialhome/federation/routed_crypto.py`). The relay can't
  derive the shared secret because it doesn't hold either ephemeral
  private half. Same idea applies to GFS-relayed public/global
  space content via `seal_for_gfs`.
- **Content key access is membership-gated.** The per-space AES-256
  content key (used by `SpaceContentEncryption` / sealed-sender) is
  delivered to a new member via the §D1b key handoff
  (`apply_space_content_key_from_metadata`) — never to a routing
  relay. Removed members lose access on the next epoch rotation
  (forward secrecy).
- **The rule is about *transport*.** Once a member receives content
  with the matching key, they decrypt and store it in their local
  database the same way every other local row is stored
  (plaintext-at-rest, under the local KEK for sensitive bytes).
  Re-encrypting at rest for receiving members is out of scope.

When adding new federation surfaces (space content, space-state mutations,
roster events) check both delivery paths. If the new event could ever
reach a non-member household, either route it through `space_instances`
or wrap with `SPACE_ROUTED` end-to-end.

### Crypto wire shapes carry a `*_suite` tag (PQ-forward by default)

Every cryptographic wire shape — signature, KEM, symmetric AEAD, key
delivery — MUST include a suite identifier so the algorithm can be
swapped without breaking older receivers. Receivers MUST reject
unknown suites; never fall back to a default.

Reference implementations:

- **Signatures:** `Encoder.sig_suite` on every envelope
  (`socialhome/federation/encoder.py`). Today `"ed25519"`. Phase-2
  PQ migration introduces `"ed25519+mldsa65"`: both signatures are
  produced + verified in parallel.
- **KEM (mesh routing):** `KEM_SUITE_X25519` in
  `socialhome/federation/routed_crypto.py`. Phase-2 introduces
  `"x25519+mlkem768"`: the pubkey field carries the concatenated
  X25519 + ML-KEM material.
- **Symmetric content key delivery:** `KEY_SUITE_AESGCM_256` in
  `socialhome/services/space_crypto_service.py`. AES-256 is
  Grover-resistant (effective ~128-bit security post-quantum); a
  PQ-protected delivery channel is the migration lever, not the
  symmetric primitive.
- **Pairing crypto:** `socialhome/federation/crypto_suite.py`
  drives the negotiate / parse / validate helpers — same pattern.

**When you add a new crypto wire format**:

1. Define a module-level `<NAME>_SUITE_<VARIANT>` constant and a
   `SUPPORTED_<NAME>_SUITES` frozenset that includes it.
2. Define an `Unsupported<Name>Suite(ValueError)` exception.
3. Ship the suite identifier on the wire (every dict the receiver
   parses; never positional / unlabelled).
4. Receivers MUST validate against `SUPPORTED_<NAME>_SUITES` and
   raise on unknown values rather than fall back to a default.
5. When the migration lands, the constant gets a sibling, the
   frozenset grows, and the new variant ships its additional
   material (concatenated pubkeys, parallel signatures, …) — the
   wire shape itself doesn't change, only its content does.

Senders that don't ship a suite field on a first-revision payload
are tolerated by defaulting to the single supported value (see
`apply_space_content_key_from_metadata` for the pattern). Once the
field is universally shipped, the default-on-missing branch
becomes the migration tripwire.

See `docs/crypto.md` for the full Phase-1 → Phase-2 PQ migration
plan. Every federation crypto wire shape now ships a suite tag —
the prior gap on `SealedEnvelope` is closed (`aead_suite` field
shipped). New cryptographic surfaces must add a sibling pattern
on the same model.

### Before adding a SQL migration, audit the code path

A migration extends the on-disk shape — it's a one-way ratchet
(rollback is operator-painful) and every deployment runs it on
startup. Before you write `socialhome/migrations/00NN_*.sql`,
prove three things to yourself in the PR description:

1. **You've audited every code path that already touches this
   data.** Grep for the existing tables/columns and read the
   handlers — particularly federation inbound, sync, and the
   migration that originally defined the column. Migrations
   often reveal that the existing shape was already trying to
   express what you need, or that a different layer (a
   federation event, a service-level cache, a derived value)
   is the right home.
2. **A non-migration alternative was considered and rejected.**
   Spell out what it would look like: reusing an existing column,
   computing a derived value at read time, storing the new state
   in a federation event instead of the table, decomposing the
   field into one that already exists. Migrations are the right
   answer often enough, but never the *first* answer — the path
   from "I need a new field" to "I'll add a column" should pass
   through "should this even be in the database".
3. **The migration is the smallest possible change.** Additive
   over destructive — `ADD COLUMN` over `RENAME TABLE`, NULL
   default over data backfill, INDEX over partial-table rewrite.
   The bar for changing existing rows is much higher than the
   bar for adding a NULL-defaulted column.

The reviewer should push back on any migration PR whose description
doesn't visibly address all three. "We extended `space_meta` instead
of adding a column" is a successful audit outcome; "we added a column
because that's where this data conceptually lives" is also valid as
long as the alternatives were genuinely considered.

This rule exists because the `socialhome/migrations/` directory is
the project's longest-running commitment to a data shape — every row
in production now and forever will pass through every migration here,
in order, on startup. Changing the shape later means *another*
migration, not an edit. Cheap to add, expensive to take back.

### Federation protocol versioning

Every confirmed peer carries a monotonic `proto_version: int` on its
`remote_instances` row, exchanged at startup via
`INSTANCE_CAPABILITIES_UPDATED`. Senders gate optional fields on
`FederationService.peer_supports(instance_id, min_version=N)` so a v_N
field never reaches a v_(N-1) peer that wouldn't know what to do with
it. See [`docs/protocol/capabilities.md`](docs/protocol/capabilities.md)
for the full design and version history.

**When you add a federation surface that an older peer cannot
fail-soft against** (a brand-new `FederationEventType`, a payload field
whose default-if-missing would be silently wrong rather than just
"unknown"):

1. Bump `OURS` in
   `socialhome/domain/federation_capabilities.py` to the next integer.
2. Add a named constant on `FederationCapability` so call sites
   reference the version by intent (`MIN_FOR_OCCURRENCE_OVERRIDE`)
   instead of a magic number. Document what v_N adds in the
   `OURS` docstring history list.
3. Gate the outbound on
   `peer_supports(peer_id, min_version=FederationCapability.X)` and
   pick a degraded fallback for older peers (or skip the send entirely
   if no safe fallback exists).
4. Append a row to the version-history table in
   `docs/protocol/capabilities.md`: what v_N changed, what an older
   peer's fallback is.
5. Update the demo harness in `.claude/skills/federation-demo/` so the
   `verify` step asserts the new version round-tripped — federation
   surfaces are easy to ship one-sided by accident.

Adding new feature flags as a per-peer string set on top of the
integer (`features TEXT NOT NULL DEFAULT '[]'`) is deliberately
deferred until we have real evidence of selective deployments,
asymmetric send/receive support, or third-party forks. Until then a
single monotonic integer covers every case; bolting flags on later is
a one-line additive migration.

### Keep docs in sync

`docs/` is the public reference for the federation protocol and the
HTTP API. Trust erodes fast when docs drift from code, so treat the
matching doc file as part of the same change.

- **Added or renamed a `FederationEventType`?** Update the `Event types`
  list on the matching page under `docs/protocol/`. If the new event
  belongs to a feature that doesn't have a page yet (new feature), add
  one — copy the shape from `docs/protocol/pairing.md` (summary, scope,
  event types, Mermaid sequence diagram, implementation pointers, spec
  refs) and link it from `docs/protocol/README.md`.
- **Changed a feature's message flow** (new signalling step, new
  `_VIA` relay, transport swap)? Update the Mermaid sequence diagram
  on the matching page. Diagrams exist to be accurate, not
  decorative.
- **Added or removed an HTTP endpoint?** Update the matching table in
  `docs/api.md`. If the endpoint is rate-limited, add a row to the
  "Rate limits" table too. If it's a new WebSocket frame type,
  document it under the WebSocket row.
- **Changed the crypto suite** (signature algorithm, key derivation,
  envelope format)? Update `docs/crypto.md`.
- **Added, dropped, or renamed a table / column / index, or shipped
  a new `0002_*.sql` migration?** Update `docs/database.md` in the
  same commit. Group the entry under the same domain heading the
  doc already uses (Identity, Spaces, DMs, …) and keep the
  one-paragraph "Purpose" shape.
- **Made an architecture-level change** (new sync tier, new resilience
  step, identity-rotation tweak, GPS-precision change, new Provider
  Protocol on `PlatformAdapter`, new platform mode)? Update
  `docs/architecture.md`. The page is the entry point for new
  contributors; outdated wiring there sends them down the wrong
  path.
- **Touched a §2 invariant** (relaxing encryption-first, allowing
  third-party trust, raising GPS precision, removing fail-closed
  behaviour)? Update `docs/principles.md` AND surface the change in
  the PR description so the reviewer flags it for explicit sign-off.
- **Changed the test strategy** (new test directory, new pytest
  marker, coverage-gate adjustment, change to the §27.9 protocol-
  test set)? Update `docs/testing.md` so the markers + commands
  there match `pyproject.toml`.
- **Added a new top-level doc file under `docs/`?** Link it from
  `docs/README.md` and add a pointer in the repo-root `README.md`
  under "Documentation".

Reviewer checklist: if a PR adds a federation event, a route, a
crypto change, a schema change, an architectural shift, a
principle change, or a test-strategy change and the docs aren't
touched, push back. The check
is "did the author update the matching doc?" — not "are the docs
perfect?" Incremental accuracy beats big bang rewrites.

### What to Never Do

- Never import inside a function or method body — all imports go at the
  top of the file. Use `if TYPE_CHECKING:` for type-only circular imports
- Never add env-var-gated stubs or dual code paths in production code to
  simplify testing. Tests mock at the test boundary (`sys.modules`
  injection or `unittest.mock.patch`). Production code always uses the
  real dependency.
- Import `aiolibdatachannel` without alias — use `import aiolibdatachannel as rtc`
- Never add `import *`
- Never commit `.env` files or secrets
- Never change the LICENSE file or SPDX identifier without explicit instruction — all source code is Mozilla Public License 2.0 (MPL-2.0)
- Never call `broadcast_to_all()` for space-scoped events — use `broadcast_to_space_members()`
- Never inline a `member.role == "subscriber"` check on a space write
  path — call `_assert_writable_member()` (or `_reject_subscriber()` if
  the member row hasn't been fetched yet) so subscriber gating stays
  uniform across post / comment / reaction / future mutations
- Never compare `member.role` against bare role strings in service
  code — import :class:`SpaceRole` from `domain.space` and use
  `SpaceRole.OWNER` / `SpaceRole.ADMIN` / `SpaceRole.MEMBER` /
  `SpaceRole.SUBSCRIBER`. The schema CHECK constraint stays the
  on-disk authority; the enum is the in-code authority
- Never store passwords, emails, phone numbers, or GPS coordinates in federation payloads
- Never bypass `_require_space_admin()` / `_require_space_member()` guards
- Never add an endpoint without a matching integration test
- Never add bootstrap logic to `run.sh` — it belongs in `ha_bootstrap.py`
- Never assume `SUPERVISOR_TOKEN` is always present — it's only set in
  `haos` mode. The Config validator enforces this at startup; route /
  service code should query `adapter.capabilities` (e.g.
  `Capability.INGRESS in caps`) instead of probing the env var
- Never branch on `config.mode` outside `platform/__init__.py` and
  `config.py` — query `adapter.capabilities` instead. The same applies to
  `isinstance`-checks against concrete adapter classes; consume the
  Provider interfaces (`adapter.auth`, `adapter.users`, `adapter.push`,
  ...) so a fourth platform can drop in without touching call sites
- Never auto-generate an admin password on first boot — the `/setup`
  wizard collects one from the operator, or `[standalone].admin_password`
  / `SH_ADMIN_PASSWORD` provides a headless override. Never log a
  generated password
- Never write a function-based route handler — use a `BaseView` subclass
  (see `routes/base.py`). Group handlers by URL resource, not by function
- Never add try/except for domain exceptions in a handler — `BaseView._iter`
  handles it centrally. Only catch exceptions that need a non-standard
  response code not covered by the base mapping
- Never write SQL directly in a route handler
- Never write SQL directly in a service — extract it into the matching
  `Abstract*Repo` + `Sqlite*Repo` (the documented exceptions are
  `backup_service` and `data_export_service`, which dump whole tables)
- **Database timestamps: store UTC, never a local time.** Two
  shapes co-exist in the codebase — SQLite's bare `datetime('now')`
  emits the naive shape `"YYYY-MM-DD HH:MM:SS"`, and Python's
  `datetime.now(timezone.utc).isoformat()` emits the tz-aware shape
  `"…+00:00"`. Both are UTC, both are fine; pick whichever the
  file you're editing already uses so a single column doesn't mix.

  Conversion to the viewer's locale is the **SPA's job** — it
  happens at the boundary, never in the repo / service / route.
  Relative labels flow through
  `client/src/utils/relativeTime.ts:parseDelta`, which already
  treats naive strings as UTC before parsing; absolute renders use
  `toLocaleString(undefined, ...)`. Federation wire payloads keep
  their tz-aware ISO 8601 shape — that's a protocol-level field
  separate from any DB column.
- Never declare a row-shaped `@dataclass` in `repositories/` — it belongs
  in `domain/`. Re-export from the repo module if existing imports need it
- Never declare a service constructor that takes `db: AsyncDatabase`
  directly when an `Abstract*Repo` already covers its needs
- Never inline service/repo construction in `create_app` — add the wiring to
  `_build_repos` / `_build_services` / `_build_middleware` factories
- Never roll your own `_running: bool` scheduler loop — copy the
  `_stop: asyncio.Event` pattern from `replay_cache_scheduler.py`
- Never call sync filesystem APIs (`open(...)`, `Path.read_bytes`,
  `Path.write_bytes`, `Path.read_text`, `Path.write_text`, `Path.is_file`,
  `Path.exists`, `Path.stat`, `Path.mkdir`, `Path.unlink`, `Path.replace`,
  `shutil.*`) from inside an `async def`. Use `aiofiles.open` for
  read/write and `aiofiles.os.*` (`makedirs`, `remove`, `replace`,
  `stat`, `path.isfile`) for metadata. The whole DM media + highlight
  streaming path was rewritten on top of this rule — any new code that
  re-introduces a sync FS call is reverting a recent fix
- Never run CPU-bound work (Pillow decode/encode, QR PNG render, gzip /
  tarfile assembly, hashlib over a multi-MiB buffer, manual format
  conversion) directly inside an `async def`. Extract the body into a
  sync helper and call it through `asyncio.to_thread(...)`. Pillow in
  particular: `media/image_processor.py` runs every upload through
  `to_thread` — match that pattern when adding new image / video / audio
  processing
- Never close the event loop on a `time.sleep(...)` — use
  `await asyncio.sleep(...)`. The single-line check matters because a
  blocking sleep in a scheduler tick stalls every other coroutine for
  the duration
- Never create a new migration without incrementing the number
- Never add / rename / remove a `FederationEventType` or an HTTP
  endpoint without updating the matching page in `docs/protocol/` or
  `docs/api.md` in the same commit. See "Keep docs in sync" above
- Never write `window.location.href = "/..."` (or `.assign`, `.replace`)
  with an absolute path in the SPA — it bypasses the HA Supervisor
  ingress prefix and bounces the iframe to HA Core's frontend. Use
  `basePath` for the app root or `addBase('/path')` for an in-app
  destination, both from `client/src/baseUrl.ts`. Same goes for
  `fetch('/api/...')`: route through the `api` client which strips
  the leading slash so `<base href>` is honoured. See
  "Frontend (SPA) & ingress" above
- Never gate `isAuthed` on `token != null` in the SPA — under haos
  the SPA carries no token (ingress headers stand in for the
  bearer). The gate is `currentUser != null`; a successful
  `/api/me` is the only signal of a working auth handshake. See
  "Frontend (SPA) & ingress" above
- Never have a setup endpoint return a bearer token in haos mode —
  ingress is the only entry point and a token would create a parallel
  auth path operators can't easily revoke. Standalone / ha setup
  endpoints DO return a token; haos setup endpoints return `{username}`
  and the SPA cold-start probes `/api/me` via ingress headers
