# DM media — pictures, videos, files

## Summary

`MESSAGE_TYPES` carries the canonical scope of a Social Home DM:

```
text · image · video · file · transcript · location
```

The first three of those — **image**, **video**, and **file** — share
one wire shape (a `media_url` plus the new `file_name` / `mime_type`
/ `file_size_bytes` metadata triple) and a two-tier delivery path
that lets cross-household attachments render *immediately* via a
small preview embedded in the encrypted envelope while the full
bytes follow on a separate
[`DM_MEDIA_BLOB`](../../socialhome/domain/federation.py) event.

## Scope

- **Federated-only.** Media attachments are allowed when the
  conversation's remote participants are all reachable through a
  *direct confirmed pairing* with the sender's household. A DM whose
  path requires the multi-hop `DM_RELAY` route (the bazaar-style
  "contact a seller through a mutual friend" flow) **rejects** media
  with `HTTP 422 MEDIA_REQUIRES_DIRECT_PAIRING`. Operator decision:
  relays are explicitly the lower-trust path and shouldn't shuttle
  picture / video / file bytes through third-party households.
- **Bytes ride encrypted.** Both the preview embedded in
  `DM_MESSAGE` and the full bytes ridden by `DM_MEDIA_BLOB` sit
  inside the §25.8.21 encryption envelope. Routing fields stay
  plaintext; everything else (filename, MIME, sizes, preview pixels)
  is encrypted under the conversation key.
- **Same processing as feed media.** Uploads ride through
  `POST /api/media/upload` (the existing endpoint posts use). Size
  caps + transcoding (`ImageProcessor`, `VideoProcessor`) come from
  [`socialhome/domain/media_constraints.py`](../../socialhome/domain/media_constraints.py).
  Image ≤ 20 MiB, longest side 2560 px → WebP @ Q82. Video ≤ 200 MiB,
  1080p, 60 sec max, CRF 25 → WebM. Files (`type='file'`) pass
  through bytes unchanged.

## Event types

| Event | When | Carries |
|---|---|---|
| `DM_MESSAGE` (v_3-shape) | Always — same envelope as a text DM | `type` ∈ {`image`, `video`, `file`}, `media_url` (local-signed for same-household, embedded preview ref for cross-household), `file_name`, `mime_type`, `file_size_bytes`, `media_blob_id` |
| `DM_MEDIA_BLOB` | Cross-household only, fired by sender's outbox after a successful `DM_MESSAGE`. May ship as one or N sequenced events — see chunking below. | `media_blob_id` (matches the `DM_MESSAGE` it follows), `message_id`, `conversation_id`, `file_name`, `mime_type`, `file_size_bytes`, `bytes_b64` (the chunk's bytes, base64), `chunk_index` (0-based), `chunk_count` (total), `final` (last chunk flag). Single-chunk legacy payloads (`chunk_count=1`) and missing-field back-compat both supported on the receiver. |

## Flow

### Same-household DM with an attachment

```mermaid
sequenceDiagram
  autonumber
  participant Alice as Alice (SPA)
  participant Backend as SocialHome backend
  participant Bob as Bob (SPA)

  Alice->>Backend: POST /api/media/upload (multipart)
  Backend->>Backend: ImageProcessor / VideoProcessor
  Backend-->>Alice: { url, signed_url, filename }
  Alice->>Backend: POST /api/conversations/{id}/messages<br/>{ type, media_url, file_name, … }
  Backend->>Backend: DmService.send_message · save row · publish DmMessageCreated
  Backend-->>Bob: WS frame dm.message (with media_url signed for Bob)
  Bob-->>Bob: bubble renders &lt;img&gt; / &lt;video&gt; / file pill inline
```

### Cross-household DM with an attachment — preview-now, sync-later

```mermaid
sequenceDiagram
  autonumber
  participant Alice as Alice (sender's HFS)
  participant Bob as Bob (receiver's HFS)

  Note over Alice: SPA upload via /api/media/upload<br/>then POST to /messages

  Alice->>Alice: Build small preview (320 px WebP @ Q60 for image; first frame extracted via PyAV for video; null for file → receiver renders glyph)
  Alice->>Bob: DM_MESSAGE (v_3, encrypted)<br/>{ media_blob_id, file_name, mime_type, preview_bytes_b64 }
  Bob-->>Bob: Save preview to local cache;<br/>bubble renders immediately with media_sync_status='pending'
  Note over Alice,Bob: ─── background ───
  Alice->>Bob: DM_MEDIA_BLOB chunks (1..N, base64; final flag on last)
  Bob-->>Bob: Each chunk → part file under media_dir;<br/>on final, concat in order + rename to <msg_id>.<ext>;<br/>update conversation_messages.media_url +<br/>media_sync_status=NULL
  Bob-->>Bob: WS push dm.media_ready → SPA<br/>swaps preview src for full media
```

### Sub-v_3 peer (§319 ¶5 `fallback`)

```mermaid
sequenceDiagram
  autonumber
  participant Alice as Alice (v_3 sender)
  participant Bob as Bob (v_2 receiver)

  Alice->>Alice: DmService.send_message · build canonical v_3 payload
  Alice->>Alice: compat.transform_for_peer(DM_MESSAGE, payload, peer_version=2)
  Note right of Alice: dm_media_v3 strips media fields,<br/>rewrites type='text', content='📎 cat.jpg — peer needs to upgrade…'
  Alice->>Bob: DM_MESSAGE (v_2-shape)
  Bob-->>Bob: regular text bubble renders normally
```

## Chunking

The federation transport (HTTPS inbox / RTC DataChannel) has a
soft ~1 MiB per-event ceiling on the serialised JSON. A 200 MiB
video would exceed that as a single base64-encoded payload, so
``DM_MEDIA_BLOB`` is split:

- Files **≤ `SINGLE_CHUNK_BYTES_THRESHOLD` (1 MiB raw)** ship as
  one event with `chunk_count=1` and `final=true`. Typical phone
  photos and short clips take this fast path — no chunking
  overhead.
- **Larger files** split into `ceil(size / MAX_BLOB_CHUNK_BYTES)`
  chunks, where each chunk carries up to 256 KiB raw (≈ 360 KB
  after base64 inflation, well under the per-event budget). Each
  chunk carries its `chunk_index`, the shared `chunk_count`, and a
  `final` flag set only on the last.

**Receiver side**: each chunk writes to `<msg_id>.part<idx>` under
the media root. When `final=true` arrives, the receiver
concatenates parts 0…N−1 in order into a temp file, atomically
renames it to `<msg_id>.<ext>`, deletes the parts, then swaps
`media_url` and broadcasts `dm.media_ready` as in the single-chunk
case. A re-send from a sender restart overwrites the same part
files idempotently; a missing chunk at finalisation time logs +
bails (the outbox retry will resend it).

**Backwards compatibility**: payloads without the `chunk_*` /
`final` fields (older builds, or any caller that doesn't emit
them) read as a single-chunk transfer — the fast path takes over.

Single envelope is the only path active for files smaller than 1
MiB; chunks above that. Chunked encryption is the same as the
non-chunked case — every envelope rides through the federation
transport's encryption layer.

## Server-side validation

- `DmService.send_message` rejects media on a relay-only conversation
  via `MediaRequiresDirectPairingError` → HTTP 422 with code
  `MEDIA_REQUIRES_DIRECT_PAIRING`. The SPA surfaces a clear copy
  ("only paired households can receive media") and keeps the
  staged attachment around so the user can drop it and resend as
  text.
- Size + MIME caps are enforced inside `MediaUploadView` before
  `media_url` ever reaches the DM POST. The DM route trusts the
  uploaded blob's signed URL.
- The receiver's `_on_dm_message` upserts on
  `conversation_messages.id` so a v_3 message arriving twice (the
  envelope + a redelivery) produces a single row.

## SPA render

[`DmThreadPage.tsx`](../../client/src/features/dms/DmThreadPage.tsx)
renders three message-bubble shapes based on `mime_type` (or the
explicit `type` when `mime_type` is `null`):

- `image/*` → `<img class="sh-message-media sh-message-media--image">`
- `video/*` → `<video class="sh-message-media sh-message-media--video" controls preload="metadata">`
- everything else → file pill (`a.sh-message-file`) with a `📎`
  glyph + filename + size, the anchor's `href` is the signed media
  URL with `download={file_name}` so the user gets a real save
  prompt.

When `media_sync_status === 'pending'`, the media gets a `--pending`
modifier class that adds a subtle brightness pulse — the visual cue
for the cross-household "preview now, full bytes coming" state.
Once the matching `DM_MEDIA_BLOB` lands, the backend's
`dm.media_ready` WS frame swaps `media_url` to the local full-bytes
URL and the modifier is removed.

## Implementation pointers

- `socialhome/domain/conversation.py` — `MESSAGE_TYPES`, the
  `ConversationMessage` dataclass with the new media columns.
- `socialhome/migrations/0003_dm_media.sql` — schema delta + the
  `dm_media_outbox` table for the cross-household scheduler.
- `socialhome/services/dm_service.py` — `send_message` accepts the
  new fields; `_reject_media_on_relay_only_conversation` enforces
  the federated-only gate; `_fan_to_remote` runs every outbound
  through `compat.transform_for_peer`.
- `socialhome/federation/compat/` — version-aware payload
  transforms. New compat tree introduced in this feature; see the
  package docstring + `dm_media_v3.py` for the canonical shape.
- `socialhome/domain/federation.py` — `DM_MEDIA_BLOB` enum entry.
- `socialhome/services/dm_media_sync_service.py` — preview
  builder, ``dm_media_outbox`` enqueue, and scheduler loop
  (asyncio.Event lifecycle per CLAUDE.md template).
- `socialhome/repositories/dm_media_outbox_repo.py` — outbox CRUD
  with exp-backoff reschedule + retry-budget exhaustion.
- `socialhome/services/federation_inbound_service.py` —
  `_receive_media_preview` decodes the inline preview at
  `DM_MESSAGE` time; `_on_dm_media_blob` writes the full bytes,
  updates `media_url`, and fans `dm.media_ready` via the realtime
  service.
- `client/src/features/dms/DmThreadPage.tsx` — paperclip attach
  button, pre-send preview tile, bubble renderers.
- `client/src/components/UploadProgress.tsx` — shared
  `uploadWithProgress` used by both feed posts and DM attachments.

## Spec refs

- §5.2 — DM scope and `MESSAGE_TYPES` definition.
- §25.8.21 — encryption-first rule: every field that isn't a routing
  primitive sits inside the encrypted payload, including the
  preview bytes and `DM_MEDIA_BLOB`'s full bytes.
- [Issue #319](https://github.com/social-home-io/socialhome/issues/319),
  paragraph 5 — per-feature degraded-shape policy. DM media: `fallback`.
