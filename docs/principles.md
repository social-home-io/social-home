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

### Sign-off: Social Home Apps execute fetched third-party JavaScript

Social Home Apps (PR1 and later) represent an **explicit, bounded
exception** to the "no third-party trust" posture: an admin may install
an app bundle that originates from the `socialhome-apps` GitHub
releases and is therefore third-party code that runs on the
household's server. Three mitigations gate this before any code executes:

1. **sha256 pinning.** The catalog entry for every app includes a
   `sha256` hex digest. `AppService` downloads the bundle tarball and
   verifies the digest before touching the filesystem — a mismatch
   aborts with an error and the bundle is never unpacked.
2. **Path-traversal guard + media containment.** Bundle entries are
   unpacked only when their resolved path stays within
   `media_path/apps/<app_id>/<version>/`. Any entry that would escape
   that directory is rejected and the whole install is rolled back.
3. **Sandboxed-iframe runtime (shipped in PR3).** App JavaScript is
   loaded into `<iframe sandbox="allow-scripts">`. The absence of
   `allow-same-origin` gives the frame an opaque origin so it cannot
   touch the parent's DOM, localStorage, cookies, or identity. A strict
   `Content-Security-Policy` (`connect-src 'none'`, `worker-src 'none'`,
   `frame-ancestors 'self'`, etc.) on every bundle response prevents
   app code from reaching the network. The host SPA validates
   `event.origin` before processing postMessage frames; the bridge is
   the only host interface and never exposes the bearer token — store
   reads/writes are proxied as per-user, server-scoped KV operations.
   The bundle is served via a signed-URL + HttpOnly path-scoped-cookie
   scheme so no credential leaks into the iframe via URL or header.

Reviewers authorising a change to `AppService` install flow, the
sandbox policy, or the postMessage bridge MUST treat any weakening of
these three mitigations as a §2-principle change requiring explicit
sign-off.

## Plaintext locally, encrypted on the wire

Local SQLite stores plaintext rows — that is your data, on your disk,
in your house. Federation envelopes are encrypted because the network
is not your house. DM end-to-end encryption is **transport-only**: the
local DB stores plaintext like every other surface, but federation
envelopes carrying DMs are encrypted such that no relay or GFS can
read them.

The same rule applies to **space content**. A household that isn't a
member of a space — but happens to be on the federation mesh between
the host and a remote member — is acting as a routing relay. It MUST
NOT be able to read any space content (posts, comments, reactions,
calendar events, location pins, …). Two layers enforce this:

1. **Direct delivery never reaches non-members.**
   `broadcast_to_space_members` targets `space_instances` (households
   with at least one member of the space), never arbitrary paired
   peers. Sending a `SPACE_*` event to a non-member household is a
   bug, not an optimization.
2. **Mesh-routed delivery seals end-to-end.** When a path between
   members crosses a non-member relay, the inner payload is wrapped
   with `SPACE_ROUTED` and sealed under the target's ephemeral
   X25519 public key (see `socialhome/federation/routed_crypto.py`).
   The relay sees the routing metadata but can't derive the shared
   secret — it holds neither ephemeral private half.

The per-space content key the recipient uses to decrypt is itself
delivered through the §D1b invite/redeem envelope (encrypted between
the host and the joining instance) — never to a routing relay.
Removed members lose access on the next epoch rotation (forward
secrecy). Once a member receives content, they store it plaintext
locally, same shape as every other surface.

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
