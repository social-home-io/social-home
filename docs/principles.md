# Design principles

These are the load-bearing decisions Social Home is built on. Every
feature decision, federation event, and data-storage choice is checked
against this list. Distilled from §2 of `spec_work.md`.

## Households first

Social Home runs **inside the household** — as a Home Assistant add-on
or as a standalone container the operator owns. There is no SaaS tier
and no centrally-hosted account system. A household's data lives on
the household's disk; nothing leaves except encrypted federation
envelopes, addressed to peers the household has chosen to pair with.

## Encryption-first (§25.8.21)

Every field in every outgoing federation event is encrypted unless the
federation service genuinely needs it in plaintext to route or
validate. Only `event_type`, `from_instance`, `to_instance`,
`space_id`, and `epoch` stay plaintext; everything else — names,
counts, choices, message bodies — sits inside the AES-256-GCM
payload. There is no `"payload": plaintext_fallback` pattern, and
there is no "trusted instance" mode that skips encryption.

## Fail closed on crypto

If `SpaceContentEncryption` isn't configured at runtime, the outbound
federation path raises `RuntimeError`. Social Home does not degrade
silently — it stops sending. The same posture applies to signature
verification on inbound: a bad signature drops the envelope on the
floor, no exceptions for "trusted" peers.

## No third-party trust

The Global Federation Server (GFS) sees **routing metadata only** —
which instance is publishing a public space, which peer is online for
push fan-out, which SDP/ICE candidates need relaying. It never sees
plaintext content, votes, names, or messages, and it cannot impersonate
a household because every payload is signed with the originating
instance's Ed25519 key (with optional ML-DSA-65 hybrid). A compromised
or malicious GFS can disrupt discovery and push, but cannot read or
forge content.

## Plaintext locally, encrypted on the wire

Local SQLite stores plaintext rows — that is your data, on your disk,
in your house. Federation envelopes are encrypted because the network
is not your house. DM end-to-end encryption is **transport-only**: the
local DB stores plaintext like every other surface, but federation
envelopes carrying DMs are encrypted such that no relay or GFS can
read them.

## Spec is the source of truth, code wins on disagreement

`spec_work.md` is the canonical specification. When code and spec
disagree, fix the code. When the architecture moves the goalposts
during implementation, fix the spec. This rule is mirrored in
`CLAUDE.md` and `AGENTS.md`; doc files (the ones you're reading) are
forward-derived from code with `§NN` backlinks to the spec.

## GPS truncation (§4 dimension)

Latitude and longitude are truncated to **4 decimal places** before
any storage or transmission. `round(float(lat), 4)` — never store raw
device precision, never cap-at-runtime-but-store-precise. Applied
uniformly: presence updates, space zones, household location, public-
space discovery rows.

## One initial migration

v1 ships exactly one schema file: `socialhome/migrations/0001_initial.sql`.
The spec's 33 numbered migrations were collapsed because there is no
migration history to preserve before v1. New schema work after v1
follows the standard `0002_*.sql` pattern.

## Layered architecture

Strict four-layer separation: **domain → repository → service → API**.
Routes are thin `BaseView` subclasses. Services depend on
`Abstract*Repo` Protocols, never on `Sqlite*Repo` concretes. SQL never
appears in services or routes — only in repositories. Domain objects
are pure dataclasses (`@dataclass(slots=True, frozen=True)`) with
behaviour as pure methods.

## Always async

All I/O is `async def`. `time.sleep()` is banned in favour of
`asyncio.sleep()`. Blocking I/O goes through `run_in_executor`. Long-
running schedulers follow the `_stop: asyncio.Event` lifecycle from
`infrastructure/replay_cache_scheduler.py`; the `_running: bool` flag
pattern is gone.

## Spec references

- §2 (design principles)
- §4 (architecture) — for "households first" topology
- §11 (instance pairing) — for the no-third-party-trust posture
- §24.11 (inbound validation pipeline) — for "fail closed on crypto"
- §25.8 / §25.8.21 (post-quantum migration + encryption-first)
