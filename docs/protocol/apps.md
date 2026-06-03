# Social Home Apps — cross-household federation

Federated app-to-app messaging that lets an installed Social Home App exchange
state with the same app running in a paired household.  A chess game, shared
whiteboard operation, or custom mini-app payload sent by Alice's household
arrives at Bob's household as an `app.message` WebSocket frame that the
host bridge relays into the running iframe — all without the app or the frame
content ever appearing in plaintext on the network.

## Scope

- **HFS**: full participant.  The `AppFederationService` drives session
  lifecycle (`APP_SESSION`) and message delivery (`APP_MESSAGE` / binary
  frames).  The `FederationService` handles encryption, signing, and the
  §24.11 inbound validation pipeline identically to every other event type.
- **GFS**: uninvolved.  App federation is strictly peer-to-peer between
  directly confirmed households.

## Event types

`APP_SESSION`, `APP_MESSAGE`.

## Transport selection

Two transport paths carry app messages; the same `session_id` scopes both:

| Path | Condition | Notes |
|---|---|---|
| **`fed-app-v1` binary DataChannel** | peer is CONFIRMED **and** channel is open **and** `peer_supports(MIN_FOR_APP_CHANNEL)` (v_17) | Fast path — no base64 overhead; high-frequency app traffic does not head-of-line-block control envelopes on `fed-v1`. |
| **`APP_MESSAGE` JSON event over `fed-v1` / HTTPS** | anything else | Fallback — identical encryption and §24.11 validation; lower throughput. |

Session control (`APP_SESSION`) always uses the JSON event path regardless of
peer version, so sessions open and close correctly even against pre-v17 peers.

## Flow — open session and send a message

```mermaid
sequenceDiagram
    autonumber
    participant A as HFS A<br/>(initiator)
    participant B as HFS B<br/>(receiver)
    participant WS_B as B local users<br/>(WebSocket)

    A->>A: AppFederationService.open_session(app_id, target={instance,user_ref,is_local})<br/>→ allocate session_id, _require_enabled check
    A->>B: APP_SESSION event<br/>{app_id, session_id, verb:"open",<br/>to_user, from_user (v_18+ peers)}<br/>encrypted + Ed25519-signed<br/>over fed-v1 / HTTPS inbox

    B->>B: §24.11 pipeline:<br/>parse → timestamp → instance → ban<br/>→ sig verify → replay → decrypt → dispatch
    B->>B: AppFederationService.on_inbound_event()<br/>→ _deliver(app_id, session_id, …)
    B->>WS_B: app.message WS frame<br/>{type:"app.message", app_id,<br/>session_id, from_instance, kind, payload}

    Note over A,B: session open; moves/ops follow

    alt fed-app-v1 binary path (v_17+ CONFIRMED direct peer)
        A->>A: FederationService.send_app_message()<br/>→ encrypt payload (AES-256-GCM)<br/>→ sign envelope<br/>→ build binary frame [u8 type][u32 hlen][header][u32 plen][payload]
        A->>B: binary frame on fed-app-v1 DataChannel
        B->>B: iter_complete_frames → §24.11 header re-validation<br/>→ decrypt payload → verify payload_sha256
        B->>B: AppFederationService.on_inbound_message()
        B->>WS_B: app.message WS frame
    else JSON fallback (sub-v17 or channel unavailable)
        A->>A: FederationService.send_app_message()<br/>→ APP_MESSAGE JSON event<br/>{app_id, session_id, data,<br/>to_user, from_user (v_18+)}<br/>encrypted + Ed25519-signed
        A->>B: APP_MESSAGE event over fed-v1 / HTTPS inbox
        B->>B: §24.11 pipeline → AppFederationService.on_inbound_event()
        B->>WS_B: app.message WS frame
    end

    A->>B: APP_SESSION event {verb:"close"}<br/>(always JSON, always encrypted)
    B->>WS_B: app.message WS frame {verb:"close"}
```

## Payload field tables

### `APP_SESSION`

| Field | Required | Notes |
|---|---|---|
| `app_id` | yes | Installed app identifier. Must match a locally-enabled app on the receiver or the inbound is silently dropped. |
| `session_id` | yes | Hex UUID allocated by the initiator.  Scopes all subsequent `APP_MESSAGE` frames for this session. |
| `verb` | yes | `"open"` / `"accept"` / `"close"`. |
| `to_user` | no (v_18+) | The addressed person's local `user_id` on the receiving household. Lets the receiver route the open to a specific user instead of fanning out to every local user. Omitted for sub-v_18 peers (legacy household fan-out). |
| `from_user` | no (v_18+) | The initiator's username — a display hint so the recipient's SPA can show who challenged them. Omitted for sub-v_18 peers. |

**v1 session lifecycle note:** the host initiates `open`; other verbs (`accept`, `close`) are passed through to the app layer — the iframe app owns session lifecycle.  The server does not enforce session state.

**Privacy note (§FIX-I2, relaxed for proto v_18):** `APP_SESSION` now carries
per-user identity (`to_user` / `from_user`) to v_18+ peers because the
challengeable roster is exactly the pairing-scoped DM / `/friends` set — a
consensual, not covert, audience.  Both fields are gated on
`FederationCapability.MIN_FOR_APP_USER_ROUTING` and omitted for sub-v_18
peers, which fall back to the legacy household-addressed fan-out (no
`to_user`).  A local-loopback open (target on the initiator's own household)
is delivered straight over WebSocket to the target and initiator only — no
federation event leaves the host.

All fields above are inside the **encrypted payload** (AES-256-GCM).  Only
`event_type`, `from_instance`, `to_instance`, `msg_id`, and `timestamp` are
in plaintext for routing.

### `APP_MESSAGE` (JSON fallback)

| Field | Required | Notes |
|---|---|---|
| `app_id` | yes | Identifies which app the data belongs to. |
| `session_id` | yes | Matches the `APP_SESSION` that opened the channel. |
| `data` | yes | Application-defined dict.  The receiver's `_deliver` passes it as-is to the `app.message` WS frame's `payload` field. |
| `to_user` | no (v_18+) | The addressed person's local username on the receiving household.  Lets `_deliver` route the message to one user instead of fanning out to every local user.  **JSON path only** — the binary `fed-app-v1` frame (v1) has no routing slot, so binary sends stay household-scoped and the receiver disambiguates by `session_id`.  Omitted for sub-v_18 peers; an empty string is treated as absent (legacy household fan-out). |
| `from_user` | no (v_18+) | The sender's username — a display hint for the receiving SPA.  JSON path only, same gating as `to_user`. |
| `app_aead_suite` | yes | Suite used for the binary payload seal (`"aesgcm-256"` today).  **Binary path only** — carried inside the envelope's encrypted metadata.  Receivers reject unknown suites via `UnsupportedAppAeadSuite`; no default fallback. |
| `payload_sha256` | yes | Base64url SHA-256 of the **plaintext** application payload.  **Binary path only** — carried inside the encrypted envelope metadata.  Binds the binary payload to the signed header; the receiver verifies `sha256(plaintext) == payload_sha256` after decryption. |

All fields are inside the encrypted payload.  `to_user` / `from_user` are
plaintext *routing* fields (usernames only, never names/PII) but, because the
whole `APP_MESSAGE` event payload is encrypted, they ride inside the ciphertext
here rather than as cleartext envelope fields.

**v1 limitation — binary fast path is household-scoped.** The binary
`fed-app-v1` frame format carries no `to_user` slot (adding one would break the
signed-bytes header contract), so a binary `send_app_message` is delivered to
every local user and the receiver scopes by `session_id` (a non-party local
user's app ignores an unknown session — harmless).  `APP_SESSION` — the
challenge/invite that drives notifications — is always JSON and **is** per-user
routed, so the important guarantee holds.  Per-user binary routing is a v2
concern.

## Binary frame format (`fed-app-v1`)

```
[u8 frame_type][u32 header_len BE][header_bytes][u32 payload_len BE][payload_bytes]
```

- `frame_type` — `1` (`FRAME_TYPE_APP_MSG`).  Unknown types are skipped for
  forward compatibility.
- `header_bytes` — the signed federation envelope JSON verbatim (the exact
  bytes that were signed).  The §24.11 pipeline re-parses this unchanged.
- `payload_bytes` — `nonce(12) ‖ AES-256-GCM(ct+tag)`.  Bound to the header
  by `payload_sha256` inside the encrypted metadata.

Payload ceiling: **1 MiB** (tighter than the media channel's 4 MiB — app
messages are chess moves and whiteboard deltas, not media blobs).  Bulk data
belongs on `fed-media-v1`.

## Encryption-first

Every application field (`app_id`, `session_id`, `verb`, `data`,
`app_aead_suite`, `payload_sha256`) is inside the AES-256-GCM encrypted
envelope payload — signed by Ed25519 and **never** in plaintext on the wire.
Only the routing envelope fields (`event_type`, `from_instance`,
`to_instance`, `msg_id`, `timestamp`) are cleartext.

For the binary path the `app_aead_suite` and `payload_sha256` binding mirror
the media channel's `media_aead_suite` / `chunk_sha256` pattern
(`federation/media_framing.py`) — tampering with the binary payload fails the
hash check; tampering with the hash fails the GCM tag or the Ed25519
signature.

## `app.message` WebSocket frame

The `AppFederationService._deliver` method broadcasts a frame to all local
WebSocket connections when an inbound app event passes the §24.11 pipeline:

| Field | Type | Notes |
|---|---|---|
| `type` | `"app.message"` | Constant WS frame type identifier. |
| `app_id` | `string` | Identifies the installed app. |
| `session_id` | `string` | Hex UUID that scopes the session — route messages to the correct in-page component using this. |
| `from_instance` | `string` | Instance ID of the sending household — lets the app show who sent a move or issued an invite. |
| `kind` | `"session"` \| `"message"` | `"session"` for `APP_SESSION` control events (open/close); `"message"` for `APP_MESSAGE` JSON events and binary `fed-app-v1` data frames. |
| `payload` | `object` | Application-defined dict. For `APP_SESSION` events this is the full decrypted payload (includes `verb`); for `APP_MESSAGE` events it is the `data` sub-field. |

### Bridge relay into the iframe

The SPA host bridge (`client/src/features/apps/bridge.ts`) listens on
`app.message` WS frames and, after filtering by `app_id`, relays the full
identity into the sandboxed iframe as a `MessageEvent`:

```ts
{
  type: 'app:event',
  kind: 'session' | 'message',   // forwarded from the WS frame
  sessionId: string,              // WS frame's session_id
  fromInstance: string,           // WS frame's from_instance
  payload: object,                // WS frame's payload
}
```

This lets apps:
- **Route by session** — use `sessionId` to dispatch a message to the correct
  game or whiteboard component when multiple sessions are open simultaneously.
- **Show sender identity** — `fromInstance` names the peer household so the
  app can display "Invited by Home B" or "Move from Home C".
- **Distinguish invites from moves** — `kind === "session"` signals a
  lifecycle event (open/close); `kind === "message"` is in-game data.

## Inbound delivery (per-user routing, v_18+)

`AppFederationService._deliver` routes an inbound app message by `to_user`:

- **`to_user` present and resolvable** — a non-empty `to_user` (carried on
  v_18+ `APP_SESSION` and JSON `APP_MESSAGE`) is resolved to a local user by
  username; the frame is delivered **only** to that user (still age-filtered).
- **absent / empty / unresolvable** — fall back to the legacy/best-effort
  **household fan-out** to every age-eligible local user.  An empty-string
  `to_user` (the back-compat household-addressed open) is treated as absent and
  is never looked up.
- **binary `fed-app-v1` path** — carries no `to_user` (v1 limitation), so it
  always uses the household fan-out; the receiver disambiguates by
  `session_id`.

If the app is not installed or not enabled on the receiving household, the
event is silently dropped with a debug log — the peer does not receive an
error.

## Target authorization

Both outbound entry points (`open_session`, `send_message`) call
`_assert_target_allowed`, which rejects any `target` that is not in the actor's
challengeable roster — the same block-aware set `list_contacts` returns
(paired-household members minus personal blocks).  A non-contact target raises
`AppContactNotFoundError`, mapped to **403 FORBIDDEN** by the route layer.
Legacy household-addressed sends (`user_ref == ""`) are exempt for back-compat.

## Implementation

- `socialhome/services/app_federation_service.py` — `AppFederationService`:
  `open_session`, `send_message`, `on_inbound_event`, `on_inbound_message`,
  `_deliver`.
- `socialhome/federation/app_framing.py` — binary frame encode/decode,
  `APP_AEAD_SUITE_AESGCM_256`, `SUPPORTED_APP_AEAD_SUITES`,
  `UnsupportedAppAeadSuite`, `CHANNEL_LABEL = "fed-app-v1"`.
- `socialhome/federation/federation_service.py` — `FederationService.send_app_message`:
  selects binary or JSON path based on `peer_supports(MIN_FOR_APP_CHANNEL)`;
  `_app_inbound_handler` for binary frame dispatch.
- `socialhome/domain/federation_capabilities.py` — `MIN_FOR_APP_CHANNEL = 17`,
  `OURS = 17`.
- `socialhome/routes/apps.py` — REST endpoints: `GET /api/apps/{app_id}/peers`,
  `POST /api/apps/{app_id}/sessions`, `POST /api/apps/{app_id}/messages`.
- `attach_apps` in `socialhome/app.py` — registers `APP_SESSION` /
  `APP_MESSAGE` handlers in the event registry and opens the `fed-app-v1`
  DataChannel on the `PeerConnection`.

## Spec references

§25 (Social Home Apps),
§24.11 (inbound federation validation pipeline),
§25.8.21 (encryption-first rule).
