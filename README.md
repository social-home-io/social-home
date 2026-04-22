# Social Home — `core`

The Python + Preact application that runs inside a household. Federates
peer-to-peer with other households, optionally subscribes to a Global
Federation Server (GFS) for public spaces, and runs equally well as a
Home Assistant add-on or as a standalone Docker container.

The full design lives in [`spec_work.md`](../../spec_work.md). When the
spec and the code disagree, the spec is the source of truth.

## Repo layout

```
social_home/             # Python backend
  app.py                 # aiohttp app factory + DI wiring
  config.py              # frozen Config dataclass (env + options.json)
  domain/                # pure dataclasses + enums + domain events
  repositories/          # SQLite-backed Protocol-style repos
  services/              # business logic (no SQL outside repos)
  routes/                # thin HTTP handlers
  federation/            # signed/encrypted federation envelope + WebRTC transport
  infrastructure/        # event bus, ws_manager, schedulers, key manager
  migrations/0001_initial.sql  # the single greenfield migration
client/src/              # Preact + TypeScript frontend
tests/                   # pytest tree mirroring social_home/
```

## Running

### Standalone (Docker)

```
docker build -t social-home:dev .
docker run --rm -p 8099:8099 -v /tmp/sh-data:/data social-home:dev
```

The first start mints an Ed25519 instance identity in `/data` and
exposes the API on `:8099`. Sign in via the login screen with a
`platform_users` row (create one with the supplied tooling — see the
spec §11 for the bootstrap flow).

### Home Assistant add-on

The Supervisor mounts `/data`, supplies `SUPERVISOR_TOKEN`, and proxies
through Ingress. Set `SH_MODE=ha` to switch the platform adapter; in
that mode the adapter reaches HA Core through the Supervisor proxy at
`http://supervisor/core/api` using `SUPERVISOR_TOKEN` — you do **not**
configure `SH_HA_URL` / `SH_HA_TOKEN`. `social_home/platform/ha_adapter.py`
handles the HA-specific bootstrap (admin provisioning, integration-token
generation, discovery push).

Run outside the Supervisor (e.g. against a dev HA instance) with
`SH_MODE=ha`, `SH_HA_URL=http://ha.local:8123`, and
`SH_HA_TOKEN=<long-lived-token>`. The same settings can live under
`[homeassistant] url=` / `token=` in the TOML file.

## Development

```
pip install -e .[dev]
pre-commit install            # ruff + mypy + frontend hooks

pytest                        # 1800+ tests, ≥90% branch coverage gate
ruff check social_home/ tests/
mypy social_home/

cd client && pnpm install && pnpm run dev   # frontend dev server
```

CI runs the same three gates plus the §27 release-blocker
[protocol tests](tests/protocol/).

## Architecture invariants

These are enforced by the linter, the mypy config, and review:

- **All I/O is async.** No `time.sleep`, no blocking calls without
  `run_in_executor`.
- **No SQL in routes.** `services/` owns business logic; `routes/` is a
  thin HTTP boundary.
- **Encryption-first** (§25.8.21). Every federation payload is encrypted
  unless the field is required for routing (`event_type`,
  `from/to_instance`, `space_id`, `epoch`).
- **GPS coordinates** are truncated to 4 decimals before storage or
  transmission.
- **Push notifications** carry only the title for DMs, location
  messages, and user-generated content (§25.3).
- **Sensitive fields** (`SENSITIVE_FIELDS` in `security.py`) never
  appear in API responses.
- **Cryptography** — see [`docs/crypto.md`](docs/crypto.md) for
  primitives, key storage, wire formats, and the post-quantum
  migration path. Full documentation index: [`docs/`](docs/).

## Spec sections

`CLAUDE.md` and `AGENTS.md` describe what AI assistants should and
shouldn't do here. The full spec is in `../../spec_work.md` (root of
the meta-repo).

## License

[Mozilla Public License 2.0](LICENSE).
