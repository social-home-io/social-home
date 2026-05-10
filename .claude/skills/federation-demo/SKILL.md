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
   - Every household sees the other two's display names via
     ``/api/friends`` (peer-directory snapshot delivered).
   - Every household sees the other two's ``all_paired`` highlights
     under ``/api/highlights``.
   - **c** has the a→c DM in ``/api/conversations`` and the message body
     round-tripped.
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
   - **Log audit**: every backend's ``log.txt`` is scanned for
     ``Traceback`` / ``ERROR:`` / ``WARNING:`` / ``Exception:``
     lines. Anything that doesn't match the benign-noise allow-list
     (``_LOG_BENIGN`` in the harness — STUN / outbox-retry /
     libdatachannel info chatter that's expected on loopback) is a
     verify failure. New unexpected logs surface in the next run as
     "log audit — …" so they get either fixed or explicitly excused.

5. ``replay`` — outbox redelivery resilience. Kills **c**, has **a**
   post one ``audience_kind=all_paired`` highlight while **c** is
   offline, restarts **c**, waits across the second outbox-backoff
   slot (~35 s), and asserts the queued highlight lands. Validates
   the §24 ``ResilientFederationOutbox`` flush-on-reachable path.

6. The harness exits non-zero if any assertion fails or any process
   crashed during the run.

The canonical ``all`` sequence runs ``up → pair → traffic →
calendar → verify → relay-pair → replay`` in that order. ``gfs-up`` /
``gfs-pair`` / ``gfs-down`` stay opt-in (they spin up a separate
GFS process and aren't required to validate the HFS↔HFS surface).

To iterate faster you can run the steps individually (``python
harness.py up`` / ``pair`` / ``traffic`` / ``calendar`` / ``verify``
/ ``relay-pair`` / ``replay``); state is persisted to
``/tmp/sh-demo/state.json`` between calls.

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
