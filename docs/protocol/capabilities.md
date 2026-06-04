# Capabilities — federation protocol version

## Why this exists

Peers run different builds. Adding a new federation surface (a new
event type, a new payload field whose default-if-missing would be
*wrong* rather than just "unknown") means the sender has to know
whether the receiver understands it. Two failure modes if it doesn't:

* **Unknown event type** — the inbound event registry returns "no
  handler" and the event is dropped at the boundary. Source and target
  end up with diverging state (e.g. one calendar shows Dec 23, the
  other still shows Dec 24).
* **Missing-default bug** — the receiver does `.get("new_field",
  default)` but the default is wrong for the new semantics (e.g. a new
  `event.privacy` field whose default would silently make everything
  public).

A single monotonic integer on each peer fixes both: the sender checks
the integer before sending and either picks a degraded shape or skips
that peer.

## On the wire

Every `remote_instances` row carries `proto_version: int`. The local
build declares its current version in
[`socialhome.domain.federation_capabilities.OURS`](../../socialhome/domain/federation_capabilities.py).
At startup the
[`CapabilitiesOutbound`](../../socialhome/services/capabilities_outbound.py)
service fans out a single envelope to every confirmed peer:

```
FederationEventType.INSTANCE_CAPABILITIES_UPDATED
payload = {"proto_version": <our int>}
```

The receiver's [pairing inbound
handlers](../../socialhome/services/federation_inbound/pairing.py)
upsert the value onto the sender's `remote_instances` row. Peers that
haven't sent the announcement yet read as `proto_version=1` — the
oldest known wire — so any gate above v1 returns `False` until the
first envelope lands.

## Sender-side gating

Outbound code consults
`FederationService.peer_supports(instance_id, min_version=N)` before
including optional v_N fields:

```python
payload = {"start": event.start, "end": event.end, ...}
if await self._federation.peer_supports(peer_id, min_version=2):
    payload["tz"] = event.tz
```

The convention is "send-the-old-shape-when-unknown" — `peer_supports`
returns `False` for any peer we don't have in `remote_instances` or
that hasn't announced yet, so the legacy shape always reaches the
peer; new fields are added only when we positively know the receiver
will parse them.

## Bumping the version

When a release adds a federation surface whose default-if-missing
would be wrong (or a brand-new event type old peers can't dispatch):

1. **Bump `OURS`** in
   [`socialhome/domain/federation_capabilities.py`](../../socialhome/domain/federation_capabilities.py)
   to the next integer.
2. **Add a named constant** to `FederationCapability` so call sites
   reference the version by intent
   (`FederationCapability.MIN_FOR_OCCURRENCE_OVERRIDE`) instead of a
   magic number. Document what changed in v_N in the history list at
   the top of that module.
3. **Pick a §319-paragraph-5 policy** for the new feature: `skip` /
   `fallback` / `force-upgrade`. Record it in the **Policy** column
   of the version history below so future contributors know what
   degraded shape (if any) old peers should receive.
4. **Wire the transform** when the policy is `fallback`: add a
   one-file shim under
   [`socialhome/federation/compat/`](../../socialhome/federation/compat/)
   that registers a per-event transform. The main service code calls
   `compat.transform_for_peer(...)` once per outbound — the rewrite
   lives entirely in the compat tree, never sprinkled across
   services. Dropping support for v_N later is then a single-file
   delete here.
5. **Update this page** with a one-line summary of v_N: what changed,
   what the older-peer fallback is.
6. **Add a test** that asserts `peer_supports` returns `False` for the
   old shape and `True` for the new one (and that the compat shim
   produces the documented fallback for a sub-v_N peer).

## Version history

| Version | What changed | Policy ([#319](https://github.com/social-home-io/socialhome/issues/319) ¶5) | Fallback for older peers |
|---|---|---|---|
| **1** | Initial wire (every event type up to but not including the calendar timezone fix). | n/a | n/a — floor. |
| **2** | `SPACE_CALENDAR_EVENT_*` and `PERSONAL_CALENDAR_EVENT_*` payloads carry an IANA `tz` field anchoring the event's wall clock. | informational | Receiver defaults `tz` to `"UTC"` — slightly wrong but not broken, so the bump is informational. |
| **3** | `DM_MESSAGE` may carry media attachments (`type` of `image` / `video` / `file`) via the existing `media_url` plus new `file_name` / `mime_type` / `file_size_bytes` / `media_blob_id` fields; the follow-up [`DM_MEDIA_BLOB`](../../socialhome/domain/federation.py) event ships the full bytes for cross-household delivery (preview-now-sync-later). | **fallback** | A sub-v_3 peer receives a synthesised `type='text'` message of the form *"📎 cat.jpg — your peer's household needs to update to share media."* — the transform lives in [`socialhome/federation/compat/dm_media_v3.py`](../../socialhome/federation/compat/dm_media_v3.py). Same-household DMs (media flows through local-signed URLs) and DMs to confirmed-paired peers (v_3) are unaffected. Media on relay-only conversations is rejected at the API boundary with `MEDIA_REQUIRES_DIRECT_PAIRING` — the relay path is explicitly lower-trust and shouldn't shuttle picture/video/file bytes through third-party households. |
| **4** | §11 bootstrap moves off the dedicated `/api/pairing/peer-{accept,confirm}` routes onto the federation inbox URL as `PAIRING_PEER_ACCEPT` / `PAIRING_PEER_CONFIRM` federation events. The receiving inbox view peeks the body's `event_type` and dispatches pairing events ahead of the §24.11 pipeline (the pipeline assumes a confirmed `RemoteInstance` that doesn't exist mid-handshake). Required so QR pairing works under HA / HAOS, where Supervisor Ingress only proxies the federation inbox path — every other route is unreachable to remote peers. | **force-upgrade** | None — the legacy `/api/pairing/peer-{accept,confirm}` routes are removed. Capability exchange happens *after* pairing completes, so the version cannot be negotiated mid-handshake; both sides must run v_4+ for pairing to complete. Sub-v_4 peers under HAOS already fail today (the bug this version fixes); sub-v_4 peers under standalone fail with a 404 on the deleted routes. |
| **5** | `LOCAL_HOME_LOCATION_CHANGED` federation event introduced. Sent from the HA / HAOS adapter (which fetches `latitude` / `longitude` from HA Core's `/api/config` on startup) to every confirmed peer whose `proto_version` is ≥ 5. Payload carries `{"latitude": <4dp>, "longitude": <4dp>}`. Receivers update `remote_instances.home_lat` / `home_lon` and publish a `PeerHomeChanged` bus event so the SPA's Connections → Map tab can pin each paired peer. Also carried in the `PAIRING_PEER_ACCEPT` body so the map is populated immediately after pairing completes (§11 bootstrap). | **skip** | Peers running v < 5 never receive the event — they keep showing the peer at its last-known coordinates (or no pin if coordinates were never exchanged). No data is lost; the location is re-broadcast automatically when the peer upgrades and the sender's next startup / location change fires. |
| **6** | Cross-instance space-invite redeem + federation mesh routing (two related additions under one release). `SPACE_INVITE_TOKEN_REDEEM` / `_ACK` / `_DENY` for receiver-initiated cross-instance redeem of `socialhome://invite#…` codes (PR 1). Generic `SPACE_ROUTED` envelope + `SPACE_FIND_ROUTE` / `SPACE_ROUTE_FOUND` for multi-hop forwarding through chains of confirmed peers (PR 2 — wraps any inner event so future event types ride mesh transparently). | **skip** | Pre-v_6 issuers can't process the redeem; the receiver-side coordinator gates the outbound on `peer_supports(min_version=6)` and 422s the SPA with a "ask the issuer to upgrade" message rather than wasting the 10 s timeout window. Same gate applies to the routing envelopes — sub-v_6 hops are excluded from discovery so a route through them is never proposed. |
| **7** | `SPACE_KEY_EXCHANGE_REKEY` is now actually shipped (PR #432, [#121](https://github.com/social-home-io/socialhome/issues/121)). Every member-removal path on the host — local kick, ban, and §D1b cross-household kick — rotates the space epoch via `SpaceContentEncryption.rotate_epoch` and broadcasts the new AES-256 content key to every remaining member household via `broadcast_to_space_members`. Without rotation, a removed member could keep decrypting future content with their cached at-rest key. The event_type itself was declared on `FederationEventType` since v_1 but no sender emitted it and no receiver registered a handler — v_7 is the version where both sides actually wire up the path. | **force-upgrade** | None — gracefully ignoring the rekey is a forward-secrecy violation, so we'd rather fail loud than silently degrade. Sub-v_7 receivers keep their old key, then fail-decrypt every subsequent `SPACE_POST_CREATED` from this host. Operators upgrading the host MUST upgrade member households together; the §25.6 direct-space-sync handshake repairs the key gap on the next sync cycle once both sides are v_7+. |
| **8** | `SPACE_MEMBER_ROLE_CHANGED` shipped (PR #434, [#114](https://github.com/social-home-io/socialhome/issues/114)). Host emits this every time an owner promotes / demotes a remote member's role; receivers update their local view of the roster (`space_members.role` for the affected member's own household, `space_remote_members.role` on witnesses). Owner role is intentionally not assignable to a remote member — ownership carries local-only privileges (dissolve, ownership transfer) that can't sensibly cross households. | **skip** | Sub-v_8 receivers silently drop the event and keep showing the pre-change role until a §25.6 sync refresh. Best-effort — a stale role badge is benign and self-heals on the next sync cycle. |
| **9** | `SPACE_REMOTE_ADMIN_KICK` shipped (PR #435, #114 phase 2). A remote admin (a member with `role='admin'` on a space hosted elsewhere) can now actually kick a member — `SpaceService.remove_member` detects when `space.owner_instance_id` is not ours and federates the kick command to the host. The host validates the actor's role from `space_remote_members.role` before dispatching to its local `remove_remote_member` / `remove_member` path (which already rotates the epoch + broadcasts the new key). Owner cannot be kicked through this path. | **force-upgrade** | Sub-v_9 hosts silently drop the command (no handler registered) — the actor's UI says "done" but the host never applies it. Operators must upgrade hosts together with members. The outbox marks this event NEVER_DROP so a temporarily-offline host eventually receives + applies the kick. |
| **10** | `BAZAAR_LISTING_CREATED` shipped (PR #445). The wrapper `PostType.BAZAAR` post already federated via `SPACE_POST_CREATED` with just the caption (`🛍 Title`); this event carries the full `BazaarListing` payload (mode, price, photos, status, …) so remote household members see what's actually for sale. Image bytes ride the existing `SpaceMediaSyncService` outbox (correlation_id = `listing.post_id`). Catch-up walks `bazaar_listings` for the space too. Status updates (sold/expired/cancelled) and bid/offer round-trips are deferred to follow-up PRs. | **skip** | Sub-v_10 peers silently see only the wrapper post (today's behaviour — `🛍 Title` with no listing card). The sender pre-filters via `peer_supports(min_version=10)` so older peers never receive a partial payload that could confuse the receiver-side decoder. Operators wanting bazaar visibility should upgrade member households alongside sellers. |
| **14** | Dedicated **binary media DataChannel** (`fed-media-v1`). A second DataChannel is negotiated on the same federation `PeerConnection`; DM + space media chunks ride it as length-prefixed binary frames (`[u8 frame_type][u32 header_len][header][u32 payload_len][payload]`) instead of base64-in-JSON on `fed-v1`. The header is the same signed federation envelope (so the §24.11 pipeline re-validates it unchanged); the payload is the AES-256-GCM-encrypted raw chunk (no base64), bound to the envelope by a `chunk_sha256` field inside the encrypted metadata. Kills the ~37 % base64 tax and stops bulk media head-of-line-blocking latency-sensitive control events. See [`media.md`](./media.md). | **fallback** | A sub-v_14 peer — or any non-CONFIRMED / mesh-only space member, since the binary channel is point-to-point — keeps receiving the existing JSON `DM_MEDIA_BLOB` / `SPACE_MEDIA_BLOB` events over `fed-v1` / HTTPS / `SPACE_ROUTED`. `FederationService.send_media_chunk` gates on `peer_supports(min_version=14)` **and** CONFIRMED status **and** an open channel, falling back transparently otherwise, so the gate degrades throughput, never correctness. |
| **13** | §25.6 chunked space sync gets two related fixes Pascal hit in production. (1) The requester now emits `SPACE_SYNC_DIRECT_READY` when the `sync-v1` DataChannel opens and `SPACE_SYNC_DIRECT_FAILED {reason: "ice_timeout"}` after the 15 s `wait_ready` deadline — before, neither was sent, so the happy path silently stalled and the existing relay-fallback hook never fired. (2) The provider's `_handle_space_sync_begin` learns the `prefer_direct=false` branch: it accepts the session in `transport_mode="https"` and streams chunks via signed `SPACE_SYNC_CHUNK` federation events instead of waiting on an SDP / ICE handshake. The receiver-side `_handle_space_sync_chunk` validates the envelope's `from_instance` matches the session's provider and forwards the inner body to the same `SpaceSyncReceiver.on_chunk` RTC frames go through. | **skip** | Sub-v_13 providers don't know how to handle `prefer_direct=false` — they accept the BEGIN and then sit idle (the bug this version fixes). The requester gates its relay retry on `peer_supports(min_version=13)` so older peers stick to direct-only; on the same LAN / behind modest NAT that still works, but cross-NAT pairs need both sides on v_13+ to get the HTTPS rescue. TURN remains the right *primary* answer for cross-NAT WebRTC — `webrtc_turn_url` flows into the same `ice_servers` list both the federation channel and the sync channel use. |
| **15** | `SPACE_REMOTE_ADMIN_ACTION` shipped (#114, cross-household admin actions). Generalises the v_9 kick: a remote admin can run **any** admin-level mutation on a space hosted elsewhere — config edit (name / emoji / features / join-mode / retention), ban / unban, archive / unarchive. The remote admin's `SpaceService` method detects `owner_instance_id != own` and, via `_forward_admin_action_if_remote`, ships a `{action, params}` intent envelope to the host instead of mutating the local stub; the host's `apply_remote_admin_action` re-validates `space_remote_members.role == admin`, whitelists the verb (+ config field names), then runs the real host method as the owner so the result federates back via the normal outbounds (`SPACE_CONFIG_CHANGED`, ban/unban, archive `space_meta`). Owner-only actions (dissolve, transfer-ownership, role assignment) are NOT forwardable and stay host-local. | **force-upgrade** | Against a sub-v_15 host (no handler) the forward raises `SpacePermissionError` instead of mutating the stub into divergence — the actor gets a clear "host needs upgrading" error rather than a silent local-only change. `_forward_admin_action_if_remote` gates on `peer_supports(min_version=15)`; the v_9 `SPACE_REMOTE_ADMIN_KICK` stays separate for back-compat so a v_9..v_14 host still honours kicks. Operators must upgrade the host before admins on other households can run these actions. |

| **16** | Multi-admin approval (quorum) for critical space actions. Dissolving a space and changing its publication tier (`space_type` → public / global) now require a *majority* of the space's admins to approve — the owner included, so no single person can unilaterally delete or publish the group. A remote admin proposes / votes via the `SPACE_REMOTE_ADMIN_ACTION` envelope (`propose` / `vote` verbs); the host re-validates the proposer/voter is a current admin (`space_remote_members.role`), recomputes the threshold after every vote (any reject cancels; a solo-admin space executes immediately), and on majority runs the real `dissolve_space` / `update_config` as the owner. The host mirrors the open proposal + tally onto admin households with `SPACE_ADMIN_PROPOSAL_UPDATED` so their SPA can render it and vote. | **force-upgrade** | A remote admin's `SpaceApprovalService` gates the forward on `peer_supports(min_version=16)` and raises (`SpacePermissionError` → 403) against a sub-v_16 host rather than dropping the proposal. Sub-v_16 *members* simply don't show the pending-proposal UI (best-effort mirror), but the host still collects votes from peers that do. Operators must upgrade the host before remote admins can propose / vote. |
| **17** | **Social Home Apps federation bridge** (`APP_SESSION` + `APP_MESSAGE`). A dedicated binary DataChannel (`fed-app-v1`) multiplexed on the same federation `PeerConnection` carries app-to-app messages as binary frames (same layout as `fed-media-v1`: `[u8 frame_type][u32 header_len][header][u32 payload_len][payload]`). The header is the signed federation envelope; the payload is AES-256-GCM-sealed, bound to the envelope by `payload_sha256` (mirrors `chunk_sha256` from the v_14 media channel). Session control (`APP_SESSION`, verb `"open"/"accept"/"close"`) always rides the JSON event path so sessions work against any confirmed peer. The suite identifier `app_aead_suite` (today `"aesgcm-256"`) is carried inside the encrypted metadata; unknown suites raise `UnsupportedAppAeadSuite` with no default fallback. No per-user identifier in `APP_SESSION` — only `session_id` + `from_instance` — to avoid cross-household user tracking (§FIX-I2). Inbound delivery: all local users with the matching app enabled receive the `app.message` WS frame (per-user routing is a documented follow-up). REST: `GET /api/apps/{app_id}/peers`, `POST /api/apps/{app_id}/sessions`, `POST /api/apps/{app_id}/messages`. | **fallback** | A sub-v_17 peer — or any peer where the `fed-app-v1` channel is not open — receives `APP_MESSAGE` as a standard JSON federation event over `fed-v1` / HTTPS inbox, AES-256-GCM encrypted as always. `FederationService.send_app_message` gates the binary path on `peer_supports(min_version=17)` **and** CONFIRMED status **and** an open channel, falling back transparently. Session control (`APP_SESSION`) always works regardless of version. |
| **18** | **Per-user app session/message routing** (`to_user` / `from_user`). `APP_SESSION` and `APP_MESSAGE` JSON payloads may now carry `to_user` (the addressee's username on the receiving household) and `from_user` (the initiator's username) so the receiver can deliver the frame to the specific challenged person rather than fanning out to all local users. Gated on `FederationCapability.MIN_FOR_APP_USER_ROUTING`; omitted for sub-v_18 peers. Local-loopback (same household) sessions are also person-addressed — the open frame goes only to the target and initiator over WebSocket, and an `AppChallengeReceived` domain event (→ bell row + title-only push per §25.3) fires for the target. New REST: `GET /api/apps/{app_id}/contacts` (person roster: local members + pairing-scoped known remote users, block-filtered). `POST …/sessions` and `/messages` accept a `target` body (`{instance_id, user_ref, is_local}`) with `peer_instance_id` as a back-compat fallback. `_assert_target_allowed` (→ `AppContactNotFoundError`, HTTP 403) gates sends to the caller's roster. §FIX-I2 relaxed: per-user identity on the wire is now permitted because the roster is gated to the consensual pairing-scoped DM/friends set (explicit maintainer sign-off). The binary `fed-app-v1` frame format (v1) carries **no** `to_user` routing slot — binary inbound always falls back to the household fan-out; the receiver disambiguates by `session_id` (documented v1 limitation). | **fallback** | Sub-v_18 peers receive the legacy household-addressed shape (no `to_user`/`from_user`); the receiver fans the event to all local users with the app enabled (the prior v_17 behaviour). The sender gates the per-user fields on `peer_supports(min_version=18)` so older peers keep working. |

## Future extensions

A *per-peer feature flag set* (a string-named set on top of the
integer) is deliberately deferred. The flag layer earns its complexity
once we have selective deployments (admin turns off a feature),
asymmetric send/receive support, or third-party forks — none of which
exist today. Adding `features TEXT NOT NULL DEFAULT '[]'` to
`remote_instances` is a one-line additive migration when real
operational evidence forces it.

Two related future events that fit the same announcement channel:

* `INSTANCE_RESYNC_REQUEST` — asks the peer to re-broadcast its state
  for a named scope (`"space:<id>"`, `"calendar:<id>"`, or
  `"capabilities"` to re-send `proto_version`). Caller fires this when
  local state is suspected stale.
* An admin-UI panel that diffs `peer.proto_version` against `OURS` per
  peer and surfaces "this peer is behind, [X] features won't work with
  them yet" hints. Pure UI on top of the existing column — no new
  wire shape. **Shipped:** the household-wide admin panel
  (`GET /api/admin/federation/compat`) plus a per-space banner
  (`GET /api/spaces/{id}/compat`, #319 ¶5) that diffs each member
  household and warns which shared-space features lag; the per-space
  subset is driven by `space_features_missing_below` over
  `SPACE_SCOPED_MIN_VERSIONS`. Both skip member households that have
  never advertised capabilities so a mid-handshake peer isn't flagged.
