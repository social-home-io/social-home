---
name: federation-demo
description: Boots four Social Home households (a / b / c / d) on adjacent ports, walks the §11 QR handshake (a↔b, b↔c, a↔c, b↔d) plus the §11 simple-pairing trust-relay flow (a auto-pairs with d via b without a QR scan), exercises the federation surface end-to-end (profile sync, posts, moments, highlights, cross-household DMs, multi-household space with remote invites, space-calendar event + cross-household RSVP) under the real WebRTC transport, and asserts that every household sees the others' federated content. Use when validating an end-to-end federation change, smoke-testing a new ``aiolibdatachannel`` release, or reproducing a multi-household sync bug.
---

## When to invoke this skill

Run this skill whenever you need to confirm the full HFS↔HFS federation
path is intact:

- Validating a federation, pairing, sync, DM-routing or space-invite
  change before merging.
- Smoke-testing a new ``aiolibdatachannel`` release before bumping the
  pin in this repo.
- Reproducing a multi-household sync bug a user has reported.

The skill expects WebRTC to work natively — no ``SH_DISABLE_RTC=1``
fallback. If the constructor segfaults on first use, your
``aiolibdatachannel`` wheel is the wrong one for the running Python /
OpenSSL combination; rebuild it from source (``pip install -e
../aiolibdatachannel --no-build-isolation``) before retrying.

## Topology

```
            ┌──── a ────┐
            │           │      a ↔ d is *not* a QR handshake — it is
            │           │      established via the §11 trust-relay
            b ◄───────► c       flow ("simple pairing" via b).
            │
            │
            d
```

- **a** — Alpha House @ ``127.0.0.1:18001`` (admin: ``alice``)
- **b** — Beta House  @ ``127.0.0.1:18002`` (admin: ``bob``)
- **c** — Gamma House @ ``127.0.0.1:18003`` (admin: ``carol``)
- **d** — Delta House @ ``127.0.0.1:18004`` (admin: ``dave``)

The inner ring (a / b / c) is fully connected via the §11 QR
handshake. **d is deliberately not paired with a directly** — only
b↔d is a QR pair. The skill then exercises the §11 simple-pairing /
trust-relay flow: Alpha asks Beta to vouch for an introduction to
Delta, Delta's admin one-clicks "accept", and the a ↔ d pair lands
without anyone scanning a QR code.

## Prereqs

- Run from the repo root (``/workspaces/social-home/repos/socialhome``).
- The Python venv (or system Python) used to launch ``socialhome`` must
  have a working ``aiolibdatachannel`` import — i.e. ``python -c "from
  aiolibdatachannel._core import PeerConnection;
  PeerConnection(ice_servers=['stun:stun.l.google.com:19302'])"``
  succeeds without segfaulting.
- Ports ``18001`` / ``18002`` / ``18003`` / ``18004`` must be free.
- ``/tmp/sh-demo`` will be wiped and re-created.

## Run it

```bash
python .claude/skills/federation-demo/harness.py all
```

That single command runs the full sequence:

1. ``up`` — wipe ``/tmp/sh-demo``, write per-instance ``socialhome.toml``
   (configures ``[standalone].external_url`` so peers can reach each
   other), launch all four backends, and walk the
   ``/api/setup/standalone`` wizard so each gets a bearer token.
2. ``pair`` — four QR handshakes (a↔b, b↔c, a↔c, b↔d). After this
   ``/api/pairing/connections`` returns the expected confirmed-peer
   counts on each instance (a:2, b:3, c:2, d:1).
3. ``relay-pair`` — §11 simple-pairing dry run.
   - ``POST /api/pairing/auto-pair-via {via_instance_id, target_instance_id}``
     on Alpha asks Beta to vouch for an introduction to Delta. Beta
     forwards the request to Delta over federation; no admin click on
     Beta's side.
   - Delta's admin sees the pending request in
     ``GET /api/pairing/auto-pair-requests`` and approves it via
     ``POST /api/pairing/auto-pair-requests/{id}/approve`` —
     one-click, no QR scan.
   - Both a and d now show each other as ``CONFIRMED``.
4. ``traffic`` — for each household (a / b / c — d stays out of the
   common content fan-out so the inner-ring assertions still bound at
   three viewers):
   - **a → b** and **c → b** ``POST /api/moments/follows`` so Beta's
     moment in the next step lands in Alpha's and Carol's inboxes.
     The follow itself federates as ``USER_FOLLOW``.
   - ``PATCH /api/me`` (display name + bio) → federates ``USER_UPDATED``
     to every paired peer.
   - ``POST /api/feed/posts`` (household-scoped, never federates — sanity
     baseline that local writes still work).
   - ``POST /api/moments`` (one per household; the public-moment ladder
     allows one new moment per 15 min). Beta's moment id is stashed in
     ``state.json`` so ``verify`` can grep it on Alpha's and Carol's
     inboxes.
   - ``POST /api/highlights/frames`` with ``audience_kind=all_paired``
     → fans out the highlight + frame to every paired peer.
   - 1:1 DM from **a → c** (cross-instance conversation; the message
     rides the federation envelope path).
   - 1:1 **media** DM from **a → c** (v_3 ``type='image'``): Alice
     uploads a tiny WebP via ``/api/media/upload``, posts a DM with
     ``media_url`` + ``file_name`` + ``mime_type``. ``DmService``
     embeds a 320 px preview in the outbound ``DM_MESSAGE``; the
     :class:`DmMediaSyncService` scheduler ships the full bytes via
     a follow-up ``DM_MEDIA_BLOB`` event. ``verify`` asserts both
     legs landed on Carol's instance.
   - **b** creates a space (``invite_only``) and mints a ``remote-invites``
     token for each of **a**'s and **c**'s admin users.
   - **b** posts a ``mode=fixed`` bazaar listing in the salon space;
     **a** opens a DM to Beta quoting the listing id ("interested in
     your bazaar listing X"). The listing itself is HFS-local
     (bazaar rows are space-scoped), but the inquiry DM rides the
     usual cross-instance ``DM_RELAY`` path.
5. ``calendar`` — cross-household space-calendar + RSVP federation.
   - Alpha and Carol accept Beta's pending remote-invites
     (``POST /api/remote_invites/{token}/accept``); both become space
     members on Beta's side.
   - Beta creates a calendar event in the space
     (``POST /api/spaces/{id}/calendar/events``); the event federates
     as ``SPACE_CALENDAR_EVENT_CREATED`` to Alpha and Gamma.
   - Alpha and Carol RSVP "going"
     (``POST /api/calendars/events/{id}/rsvp``); each RSVP federates
     back to Beta as ``SPACE_CALENDAR_RSVP``.
   - Beta's ``GET /api/calendars/events/{id}/rsvps`` is asserted to
     show both Alpha and Carol as ``going``.

4. ``verify`` — assertions across all three households:
   - Every confirmed peer advertises the build's current ``OURS``
     ``proto_version`` (the capability-bump tripwire — now v_21, which adds
     authenticated mesh route discovery: a v_21 target signs the
     ``target_eph_pk`` it ships in ``SPACE_ROUTE_FOUND`` so the origin won't
     seal space content under a relay-substituted key. v_20 guards the
     ``SPACE_SYNC_REJECTED`` reconnect-reconcile backstop: a host only sends
     that reply to peers it sees at >= v_20), and an
     ``INSTANCE_RESYNC_REQUEST`` (capabilities scope, v_19, #319 ¶6) to a
     v_19+ peer via ``POST /api/admin/federation/resync`` is accepted —
     proving the new event type + operator endpoint + ``peer_supports``
     gate round-trip (a sub-v_19 peer would 409).
   - Every household sees the other two's display names via
     ``/api/friends`` (peer-directory snapshot delivered).
   - Every household sees the other two's ``all_paired`` highlights
     under ``/api/highlights``.
   - **c** has the a→c DM in ``/api/conversations`` and the message body
     round-tripped.
   - **c** also has the v_3 cross-household media DM from **a**:
     after a short scheduler-flush grace period the row's
     ``type=image``, ``media_sync_status`` is cleared, and the
     receiver-side ``media_url`` resolves on Carol's instance —
     i.e. the ``DM_MESSAGE`` preview arrived AND the follow-up
     ``DM_MEDIA_BLOB`` landed the full bytes under Carol's
     ``media_dir``.
   - **b** has the a→b bazaar-inquiry DM in ``/api/conversations``,
     and the message body still quotes the listing id (i.e. the body
     made it through ``DM_RELAY`` decryption intact).
   - **a** and **c** have **b**'s moment in their ``/api/moments``
     inbox (validates ``MOMENT_CREATED`` outbound + the inbox-
     fan-out mirror against ``moment_follows``).
   - **b**'s space membership is queryable (Alice / Carol show as
     pending or joined depending on whether the invitation flow has
     auto-accepted by the time the assertion runs).
   - Every backend process is still alive (no WebRTC native crash).
   - **Home-location propagation** (v5): each of a / b / c sees every
     confirmed peer's ``home_lat`` / ``home_lon`` populated in
     ``/api/friends``. Coordinates are seeded in ``cmd_up`` (Berlin /
     Hamburg / Frankfurt / Munich) and carried during the §11
     pairing handshake via the ``peer-accept`` body. A NULL coord
     on the peer row means the pairing carry-through regressed.
   - **share_home toggle** (per-peer home-sharing control): flips Alpha's
     ``share_home`` for Bob to OFF via
     ``PATCH /api/pairing/connections/{bob_id}`` ``{"share_home": false}``,
     waits ~1 s for the null-coord ``LOCAL_HOME_LOCATION_CHANGED`` envelope
     to land, then asserts Bob's ``remote_instances.home_lat`` / ``home_lon``
     for Alpha's row are NULL in Bob's DB. Flips back ON and asserts the
     coords are restored. Exercises ``PeerHomeSharingService.set_share_home``
     end-to-end.
   - **User preferences round-trip**: PATCHes ``/api/me/preferences``
     with ``{hide_highlights: true}`` as Alice, re-fetches
     ``GET /api/me/preferences``, and asserts the value persisted.
     Then checks Bob's preferences via his own token and asserts
     ``hide_highlights`` is still false — no cross-talk between users.
     Alice's preference is restored to false before the step exits.
   - **Log audit**: every backend's ``log.txt`` is split into
     per-record blocks (one block per non-indented line plus its
     indented traceback frames), then scanned for ``Traceback`` /
     ``ERROR:`` / ``WARNING:`` / ``Exception:`` markers. A block
     stays suppressed only when its full text matches at least one
     substring in ``_LOG_BENIGN`` (the harness's allow-list, which
     is comment-heavy so a future contributor can see why each
     pattern is intentional). New unexpected logs surface in the
     next run as ``<inst>: log audit — …`` and become verify
     failures — either fix the root cause OR add an allow-list
     entry with a comment explaining why it's benign.

     The block splitter explicitly distinguishes Python logging
     records (``LEVEL:logger:msg``) from exception summaries
     (``ValueError: …``) so consecutive ``ERROR:`` /
     ``WARNING:`` lines each become their own block. Without that
     guard the whole log collapses to one block and the audit
     misses real per-line errors — pinned by
     ``tests/skills/test_federation_demo_audit.py`` so a future
     refactor can't silently regress the splitter again.

     What the audit currently accepts as benign (each with an
     in-source comment): STUN-server status chatter, outbox-retry
     on briefly-unreachable peers, outbox terminal drops on HTTP
     410 (dual-transport replay-cache hit), HTTPS-inbox transient
     failures during the inner-ring settle window, DTLS handshake
     timeouts on loopback (perfect-negotiation glare aborts one
     side mid-handshake, federation falls through to HTTPS-inbox),
     ICE candidate drops after the 30 s buffer window, and the
     rate-limit middleware's own announcement when ``relay-pair``
     waits the 65 s window. If any of these patterns START coming
     with a per-step content-check failure, that's the real bug
     — the log line on its own is just the symptom.

5. ``visibility`` — per-pair user-visibility filter. Provisions a
   second local user on **a** (``ada``), confirms **b** mirrors her
   in ``/api/friends``, then hides ada from **b** via
   ``PATCH /api/pairing/connections/{b_id}/visible-users``. Asserts
   **b**'s mirror drops ada. **While ada is hidden** the step also
   fires a DM (ada → bob), a moment (ada), and an ``audience_kind=
   all_paired`` highlight (ada) and asserts **none** of them reach
   **b** — exercising the DM_MESSAGE, MOMENT_CREATED, and HIGHLIGHT_*
   outbound gates. Gamma is the positive control: ada is **not**
   hidden from **c**, so the same highlight is asserted to land on
   Gamma's ``/api/highlights``. Then flips ada back to visible and
   asserts **b** sees her again.

6. ``invite-redeem`` — cross-instance space-invite token redeem
   over federation. Carol creates a private space on **c**, mints
   an invite token, Alice POSTs the token to her own
   ``/api/spaces/join`` with ``issuer_instance_id=<carol's id>``.
   The backend routes the redeem over
   ``SPACE_INVITE_TOKEN_REDEEM``; Carol's instance validates the
   token, seats Alice as a remote space member, ACKs back. The
   harness asserts both legs: Alice's HTTP response carries the
   right ``space_id`` (proves the ACK round-tripped), and Carol's
   ``space_remote_members`` DB row exists (direct check —
   ``/api/spaces/{id}/members`` only surfaces local members).
   PR 1 baseline for the relayed case (a wants to join d's space
   via b) that lands as ``invite-redeem-routed`` in step 7.

7. ``invite-redeem-routed`` (PR 2, v_6 mesh) — c (receiver) joins
   a space hosted on d (issuer) but is **not** directly paired with
   d; the only path is c↔b↔d. The receiver-side coordinator runs
   ``SPACE_FIND_ROUTE`` to discover the path, gets back d's per-
   route X25519 ephemeral pub via ``SPACE_ROUTE_FOUND``, and ships
   the redeem as a ``SPACE_ROUTED`` envelope with the inner
   payload sealed end-to-end (HKDF-derived directional AES-256-GCM
   key, AAD bound to ``route_id`` + ``inner_event_type``). b
   forwards the opaque ciphertext without decrypting it; d unseals,
   seats c as a remote member, and ships the ACK back as
   ``SPACE_ROUTED(direction=reply)``. Asserts: c's
   ``POST /api/spaces/join`` resolved 200/201 (full round-trip
   completed), ``d.space_remote_members`` contains c (inner REDEEM
   dispatched at the target after unseal), AND b's log contains
   ``SPACE_ROUTED`` but **not** ``SPACE_INVITE_TOKEN_REDEEM``
   (encryption invariant — relays never see the inner event_type).

8. ``remote-invite-routed`` (PR 3, v_6 mesh-private-invite) — c
   (admin) invites dave on d through the unpaired path c↔b↔d.
   Backend ``SpaceService.invite_remote_user`` runs route discovery
   and ships ``SPACE_PRIVATE_INVITE`` via ``SPACE_ROUTED(forward)``;
   b forwards the opaque blob without decrypting. d's inbox surfaces
   the invite via ``GET /api/remote_invites``; d accepts;
   ``SpaceService.accept_remote_invite`` runs a *fresh* discovery
   (the original reply-leg ephemerals have expired in the user-time
   gap) and ships ``SPACE_PRIVATE_INVITE_ACCEPT`` as a new
   ``SPACE_ROUTED(forward)`` leg back through b. Asserts: invite
   POST returns 201 (mesh send succeeded), d's inbox surfaces the
   row, accept returns 200/204, ``c.space_remote_members`` shows
   dave, AND b's log contains ``SPACE_ROUTED`` but **not**
   ``SPACE_PRIVATE_INVITE_ACCEPT`` (encryption invariant — relays
   cannot read the admin-initiated flow either).

9. ``remote-invite-decline`` (PR 3, decline-path coverage) — c
   invites alice (direct pair), alice declines via
   ``POST /api/remote_invites/{token}/decline``,
   ``SPACE_PRIVATE_INVITE_DECLINE`` round-trips to c. Asserts the
   ``space_invitations`` row on c moves to ``status='declined'``
   AND alice is NOT seated in ``c.space_remote_members``
   (defensive: a decline must not accidentally seat the user).
   Complements the accept-only path that
   ``cmd_traffic`` + ``cmd_calendar`` exercise today.

10. ``replay`` — outbox redelivery resilience. Kills **c**, has **a**
   post one ``audience_kind=all_paired`` highlight while **c** is
   offline, restarts **c**, waits across the second outbox-backoff
   slot (~35 s), and asserts the queued highlight lands. Validates
   the §24 ``ResilientFederationOutbox`` flush-on-reachable path.

8. The harness exits non-zero if any assertion fails or any process
   crashed during the run.

The canonical ``all`` sequence runs ``up → pair → traffic →
calendar → verify → relay-pair → visibility → invite-redeem →
invite-redeem-routed → remote-invite-routed → space-post-routed →
space-media-blob → space-gallery-media-blob →
space-sync-catchup-media → sync-https-fallback → admin-promote-kick →
app-session → remote-invite-decline → replay`` in that order.
``gfs-up`` / ``gfs-pair`` / ``gfs-down`` stay opt-in (they spin up
a separate GFS process and aren't required to validate the HFS↔HFS
surface).

Phases added after the initial publish are documented inline in
``harness.py`` (each ``cmd_*`` has its own docstring):

* ``space-post-routed`` — mesh-routed SPACE_POST_CREATED via
  SPACE_ROUTED through a relay that never decrypts the inner
  payload.
* ``space-media-blob`` — bytes for a posted image actually reach
  the remote member's media path (the SpaceMediaSyncService
  outbox + chunked SPACE_MEDIA_BLOB stream).
* ``space-gallery-media-blob`` — same as above, exercised through
  the gallery upload path. Thumbnail + full bytes both land on
  the remote member's media path via the shared media outbox.
* ``space-sync-catchup-media`` — newcomer joining a long-running
  space gets the historical post + gallery bytes too, not just
  the metadata rows. Catch-up enqueues happen after the §25.6
  metadata sentinel; assertion proves both surfaces land on the
  joiner's media path.
* ``admin-promote-kick`` — cross-household role promotion lands on
  the affected member's household via SPACE_MEMBER_ROLE_CHANGED.
* ``app-session`` — app-to-app federation (v_17+/v_18): opens an
  ``APP_SESSION`` from a to b via the legacy ``peer_instance_id``
  path, sends an ``APP_MESSAGE``, and asserts both REST calls return
  2xx.  Also exercises the v_18 additions: probes
  ``GET /api/apps/{id}/contacts`` and validates the contact shape;
  when remote contacts on b are present, opens a person-routed
  session via the new ``target`` body and asserts the 201 response.
  Skips gracefully when no common installed app is present in the
  demo environment (unit tests in
  ``tests/services/test_app_federation_service.py`` cover the full
  delivery path with a WS mock).

To iterate faster you can run the steps individually (``python
harness.py up`` / ``pair`` / ``traffic`` / ``calendar`` / ``verify``
/ ``relay-pair`` / ``visibility`` / ``invite-redeem`` /
``invite-redeem-routed`` / ``remote-invite-routed`` /
``space-post-routed`` / ``space-media-blob`` /
``space-gallery-media-blob`` / ``space-sync-catchup-media`` /
``sync-https-fallback`` / ``admin-promote-kick`` / ``app-session`` /
``remote-invite-decline`` / ``replay``);
state is persisted to ``/tmp/sh-demo/state.json`` between calls.

## GFS (Global Federation Server) — opt-in subcommands

The skill also wires up the **GFS** path as a separate, opt-in flow
on top of the canonical HFS-only ``all`` run. Boot a GFS, pair Alpha
+ Delta with it, and tear down — the full HFS↔GFS pair handshake
runs end-to-end against a real GFS process.

```bash
python .claude/skills/federation-demo/harness.py up
python .claude/skills/federation-demo/harness.py gfs-up
python .claude/skills/federation-demo/harness.py gfs-pair
python .claude/skills/federation-demo/harness.py gfs-traffic
python .claude/skills/federation-demo/harness.py gfs-replay
python .claude/skills/federation-demo/harness.py gfs-down
```

### ``gfs-up``

Starts a Global Federation Server on ``127.0.0.1:18765``. Bypasses
the interactive ``socialhome-global-server --init / --set-password``
CLI: writes the example TOML, points ``base_url`` and ``data_dir`` at
the local sandbox, seeds a bcrypt admin-password hash, and spawns
``python -c "from socialhome.global_server.server import main; main()"``
under the hood. Verifies the GFS is reachable by polling ``/healthz``.

State is persisted under ``/tmp/sh-demo/gfs/`` (same parent as the
HFS sandboxes); ``gfs-down`` (or the broader ``down``) tears it down.

### ``gfs-pair``

Walks the §24 GFS pairing handshake for Alpha and Delta against the
running GFS:

1. Mint a one-time pair token via the GFS landing page (the QR
   token; rendered as ``data-pair-token`` on the ``<code>`` element
   so it's scrape-friendly).
2. POST it to the SH side's ``/api/gfs/connections``. The SH then
   ``GET {gfs_url}/gfs/info`` to pull the GFS's Ed25519 ``public_key``
   and ``POST /gfs/register`` with the SH's ``{instance_id,
   public_key, inbox_url, token}`` body.
3. Assert both households now show the GFS connection as
   ``status="active"`` (auto-accept is on by default for fresh
   deployments).

### ``gfs-traffic`` — global-space publish round-trip

Validates the **publish** wire end-to-end: Alpha creates a
``space_type=global`` space, ``SpaceService._auto_publish_on_type``
fires, ``GfsConnectionService.publish_space`` builds a metadata
payload, signs it with Alpha's identity key, and POSTs to
``/gfs/spaces/{space_id}/publish``. The GFS verifies the
signature against the registered ``ClientInstance.public_key``,
upserts a ``GlobalSpace`` row at ``status='active'``, and the
harness then asserts the space appears on ``GET /gfs/spaces``
with the right name + owning instance.

Prerequisites: ``up`` + ``gfs-up`` + ``gfs-pair`` (Alpha must be
a paired client of the GFS so the registration sig verifies).

### ``gfs-replay`` — owning-HFS downtime survives the publish

Validates that a GFS publication is durable across an owning-HFS
restart. Runs after ``gfs-traffic`` (which already published Alpha's
global space). Sequence:

1. Pre-check: ``GET /gfs/spaces`` lists Alpha's space, and Alpha's
   ``/api/gfs/publications`` mirrors the same row.
2. SIGTERM Alpha; wait for the process to exit.
3. While Alpha is offline, the GFS continues to list the space — the
   GFS does not proactively unpublish on owning-instance disconnect.
4. Respawn Alpha on the same data_dir; wait for
   ``/api/instance/config`` to answer 200.
5. Wait ~8 s for the ``GfsWebSocketSupervisor`` to reconcile its
   per-connection clients and the ``GfsWebSocketClient`` to reopen
   ``wss://gfs/gfs/ws``.
6. Re-assert: Alpha's ``/api/gfs/connections`` shows the connection
   active, ``/api/gfs/publications`` still lists the space, and
   ``GET /gfs/spaces`` still lists it.

Failure modes this catches:

- The GFS-side ``GlobalSpace`` row gets garbage-collected on
  owning-HFS disconnect (regression).
- ``GfsWebSocketSupervisor.start()`` is no longer fired from
  ``_on_startup`` (regression — the WS would never reconnect).
- The publication mirror in ``gfs_publications`` gets wiped by a
  migration or a misfired ``unpublish_space_from_all`` on shutdown.

### Cross-household post / bazaar / public moment over GFS (TODO)

Still to wire end-to-end:

- A discovery-side mirror (subscriber fetches ``/gfs/spaces/{id}``
  for full metadata, creates a stub ``spaces`` row) so
  ``subscribe_to_space`` accepts the join.
- ``POST /api/feed/posts`` against a global space federating as
  ``SPACE_POST_CREATED`` via the GFS WebSocket relay (the
  ``publish_event`` path already exists; just needs a subscriber
  on the receiving side).
- ``POST /api/bazaar/listings`` for the Bazaar test path.
- ``POST /api/moments`` with ``is_public=true`` for the public-
  moment / GFS-following test path.

## Troubleshooting

- **PeerConnection segfaults on the very first call** — the
  ``aiolibdatachannel`` wheel installed in the active Python is the
  pre-OpenSSL-3 release. Either upgrade to ``aiolibdatachannel >=
  2026.5.9`` or rebuild from source against the system OpenSSL 3:
  ``pip install -e /workspaces/social-home/repos/aiolibdatachannel --no-build-isolation``.
- **``setup_required: false`` on a fresh DB** — a previous instance is
  still running. Run ``python harness.py down`` first, then ``up``.
- **``federation inbox: rejected reason=Failed to decrypt payload``** —
  one of the three ``[standalone].external_url`` values doesn't match
  what peers can actually reach (the harness uses ``127.0.0.1`` so this
  shouldn't happen unless ports are stomped).
- **DM a→c isn't visible on c after ~15 s** — check ``/tmp/sh-demo/c/log.txt``
  for ``federation inbox: rejected`` lines. The DM message rides
  federation envelopes; a stale outbox entry can stall delivery — the
  harness waits 5 s before asserting which is enough on a quiet host.

## Cleanup

```bash
python .claude/skills/federation-demo/harness.py down
```

Sends ``SIGTERM`` (then ``SIGKILL`` after 1 s) to each process group
and removes ``/tmp/sh-demo``.

## Files

- [`harness.py`](harness.py) — the driver script. Contains all the
  ``up``/``pair``/``traffic``/``verify``/``down`` subcommands plus the
  ``all`` orchestrator. Each subcommand persists / loads state from
  ``/tmp/sh-demo/state.json`` so you can re-run individual steps.
