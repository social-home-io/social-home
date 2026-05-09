---
name: federation-demo
description: Boots three Social Home households (a / b / c) on adjacent ports, walks the §11 QR pairing handshake between every pair, exercises the federation surface end-to-end (profile sync, posts, moments, highlights, DMs across instances, multi-household space with remote invites) under the real WebRTC transport, and asserts that every household sees the others' federated content. Use when validating an end-to-end federation change, smoke-testing a new ``aiolibdatachannel`` release, or reproducing a multi-household sync bug.
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
        │           │
        │           │
        b ◄────► c
```

- **a** — Alpha House @ ``127.0.0.1:18001`` (admin: ``alice``)
- **b** — Beta House  @ ``127.0.0.1:18002`` (admin: ``bob``)
- **c** — Gamma House @ ``127.0.0.1:18003`` (admin: ``carol``)

All three pairs are mutually paired via the §11 QR handshake. The
naming "a ↔ b ↔ c" in conversation refers to *intent* (b sits in the
middle of the social graph), not topology — every pair is a direct
peer so DMs, highlight federation, and remote-invite acceptance don't
depend on relay.

## Prereqs

- Run from the repo root (``/workspaces/social-home/repos/socialhome``).
- The Python venv (or system Python) used to launch ``socialhome`` must
  have a working ``aiolibdatachannel`` import — i.e. ``python -c "from
  aiolibdatachannel._core import PeerConnection;
  PeerConnection(ice_servers=['stun:stun.l.google.com:19302'])"``
  succeeds without segfaulting.
- Ports ``18001`` / ``18002`` / ``18003`` must be free.
- ``/tmp/sh-demo`` will be wiped and re-created.

## Run it

```bash
python .claude/skills/federation-demo/harness.py all
```

That single command runs the full sequence:

1. ``up`` — wipe ``/tmp/sh-demo``, write per-instance ``socialhome.toml``
   (configures ``[standalone].external_url`` so peers can reach each
   other), launch all three backends, and walk the ``/api/setup/standalone``
   wizard so each gets a bearer token.
2. ``pair`` — three QR handshakes (a↔b, b↔c, a↔c). After this every
   ``/api/pairing/connections`` returns two ``confirmed`` peers.
3. ``traffic`` — for each household:
   - ``PATCH /api/me`` (display name + bio) → federates ``USER_UPDATED``
     to every paired peer.
   - ``POST /api/feed/posts`` (household-scoped, never federates — sanity
     baseline that local writes still work).
   - ``POST /api/moments`` (one per household; the public-moment ladder
     allows one new moment per 15 min).
   - ``POST /api/highlights/frames`` with ``audience_kind=all_paired``
     → fans out the highlight + frame to both peers.
   - 1:1 DM from **a → c** (cross-instance conversation; the message
     rides the federation envelope path).
   - **b** creates a space (``invite_only``) and mints a ``remote-invites``
     token for each of **a**'s and **c**'s admin users.

4. ``verify`` — assertions across all three households:
   - Every household sees the other two's display names via
     ``/api/friends`` (peer-directory snapshot delivered).
   - Every household sees the other two's ``all_paired`` highlights
     under ``/api/highlights``.
   - **c** has the a→c DM in ``/api/conversations`` and the message body
     round-tripped.
   - **b**'s space membership is queryable (Alice / Carol show as
     pending or joined depending on whether the invitation flow has
     auto-accepted by the time the assertion runs).
   - Every backend process is still alive (no WebRTC native crash).

5. The harness exits non-zero if any assertion fails or any process
   crashed during the run.

To iterate faster you can run the steps individually
(``python harness.py up`` / ``pair`` / ``traffic`` / ``verify``); state
is persisted to ``/tmp/sh-demo/state.json`` between calls.

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
