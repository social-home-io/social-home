# Public Momentum (§Momentum-public)

Opt-in extension to the [Momentum](momentum.md) pillar that lets a
registered user fan their moments out beyond the household-paired
mesh, brokered by a GFS. Followers across the public internet
discover authors via the GFS directory, follow them explicitly, and
receive every subsequent moment over the same persistent SH↔GFS
WebSocket the rest of the public-pillar surface (Highlights) already
uses.

## Scope

* Per-user opt-in. The author of each moment chooses whether to fan
  it out via their registered GFSes; an opt-out checkbox at compose
  time overrides the per-user default.
* Multi-GFS publishing. One user can register on several GFSes at
  once. The fan-out posts to each in turn.
* Follower-side gate. The GFS only forwards a moment to households
  that have at least one follower of the author. Strangers see
  nothing.
* **No re-distribute rule.** A moment that arrived via a GFS
  fan-out is *not* relayed back into the recipient's paired
  household mesh. The federation outbound (`MomentFederationOutbound.relay_inbound`)
  reads `received_via='gfs'` on the inbound payload and skips relay.
* Replies fan out **both** through the GFS path and through the
  replier's paired households (normal moment federation).
* Author-side moments still fan out to paired households via the
  existing federation alongside the GFS path. Recipients dedupe by
  `moment.id` (the `moments` PRIMARY KEY makes the second save a
  no-op).

## Encryption posture

Per §25.8.21 (encryption-first), the GFS sees the absolute minimum
needed to route. For public Momentum:

* The author's instance **signs** the moment envelope (Ed25519 over
  the canonical JSON of every field except ``signature``).
* The GFS holds plaintext **only in memory** during fan-out. It is
  never persisted on the GFS — `gfs_moments` does not exist as a
  table; the broker reads the envelope, looks up follower instances,
  pushes the frame to each, and discards.
* Recipients verify the signature against the author's
  `home_instance_pk` cached in `moment_public_follows` at follow
  time. A bad signature drops the frame on the floor.

## Event types

| Event type (federation) | Direction | Purpose |
|---|---|---|
| `MOMENT_PUBLIC_FOLLOW` | GFS → author | Follower count tick. |
| `MOMENT_PUBLIC_UNFOLLOW` | GFS → author | Follower count tick. |

The moment payload itself rides as a **WS frame**, not a federation
event — the GFS is the validated middlebox and the recipient
verifies the author's signature directly.

| WS frame type | Direction | Carries |
|---|---|---|
| `moment_public` | author SH → GFS (HTTP `POST /gfs/moments/publish`) | Signed moment envelope. |
| `moment_public_delete` | author SH → GFS (HTTP `POST /gfs/moments/delete`) | Signed tombstone. |
| `incoming_public_moment` | GFS → follower SH | Forwarded envelope, signature intact. |
| `incoming_public_moment_delete` | GFS → follower SH | Forwarded tombstone. |
| `follow_changed` | GFS → author SH | Notifies the author about follower count change. |

## Wire endpoints

### GFS-side

| Method | Path | Purpose |
|---|---|---|
| POST   | `/gfs/users/register` | Signed body — register a user. |
| POST   | `/gfs/users/{user_id}/deregister` | Signed body — drop a registration. |
| POST   | `/gfs/users/{user_id}/follow` | Signed body — record a follower. |
| POST   | `/gfs/users/{user_id}/unfollow` | Signed body — drop a follower. |
| POST   | `/gfs/moments/publish` | Signed envelope — fan out to followers. |
| POST   | `/gfs/moments/delete` | Signed tombstone — fan out the delete. |
| GET    | `/gfs/users` | Public JSON directory. |
| GET    | `/moments` | Public HTML directory. |
| GET    | `/moments/{user_id}` | Per-user public HTML landing. |

### SH-side (auth-gated)

| Method | Path | Purpose |
|---|---|---|
| GET    | `/api/moments/public/registrations` | Caller's registrations. |
| POST   | `/api/moments/public/registrations` | Register on a GFS. |
| DELETE | `/api/moments/public/registrations/{gfs_id}` | Deregister. |
| PATCH  | `/api/moments/public/registrations/{gfs_id}` | Toggle `default_share`. |
| GET    | `/api/moments/public/follows` | Caller's GFS follows. |
| POST   | `/api/moments/public/follows` | Follow another author. |
| DELETE | `/api/moments/public/follows/{gfs_id}/{user_id}` | Unfollow. |
| GET    | `/api/gfs/{gfs_id}/users` | Proxy the GFS directory. |

## Sequence: discover + follow

```mermaid
sequenceDiagram
  participant B as Follower SH
  participant G as GFS
  participant A as Author SH

  B->>G: GET /gfs/users (anonymous)
  G-->>B: directory listing (incl. home_instance_pk per user)
  B->>G: POST /gfs/users/{user_id}/follow (signed by B's instance)
  G->>G: persist gfs_moment_follows row
  G->>B: response carries the author's directory entry
  B->>B: cache row in moment_public_follows (with home_instance_pk)
  G->>A: WS push follow_changed (action="add")
  A->>A: composer follower count ticks up
```

## Sequence: publish + fan-out

```mermaid
sequenceDiagram
  participant A as Author SH
  participant G as GFS
  participant B as Follower SH
  participant P as A's paired peer

  A->>A: User posts moment (is_public=1)
  par Household path (existing federation)
    A->>P: MOMENT_CREATED via paired-instance relay
  and GFS path (new)
    A->>G: POST /gfs/moments/publish (signed envelope)
    G->>G: lookup followers_of(author)
    loop per unique follower_instance_id
      G->>B: WS push incoming_public_moment
    end
    B->>B: verify Ed25519 signature against cached followed_instance_pk
    B->>B: persist moment (received_via='gfs', received_via_gfs_id={gfs})
    B->>B: re-publish MomentCreated on bus → realtime + notifications
    Note right of B: relay_inbound() short-circuits on received_via=='gfs'
  end
```

If A's paired-household mesh and B's mesh overlap (same household
pair), B receives the same ``moment_id`` twice — once via federation,
once via GFS. The recipient dedupes on the row's PRIMARY KEY; the
second save is a no-op.

## Signature canonicalisation

Every signed wire body — register, follow, unfollow, publish, delete
— uses the same canonical-JSON shape:

```python
canonical = json.dumps(
    body, separators=(",", ":"), sort_keys=True
).encode("utf-8")
```

The Ed25519 signature is computed over those bytes, base64url-encoded
(no padding), and appended to the body under the ``signature`` key.
The ``signature`` field is **excluded** from the canonical bytes —
the verifier strips it back out before re-running ``json.dumps`` to
compare.

* **Algorithm**: Ed25519 (per §25.8 instance-key suite).
* **Key**: the sending instance's federation identity key (32-byte
  Ed25519 seed). For follower-side calls (``follow`` / ``unfollow``)
  the follower's *own* instance signs; for publish / delete the
  author's instance signs.
* **Public key encoding**: 64-character lowercase hex on every
  GFS-stored or wire-carried form (``client_instances.public_key``,
  ``gfs_user_registrations.home_instance_pk``,
  ``moment_public_follows.followed_instance_pk``). The choice of hex
  for keys + base64url for signatures matches the existing
  Highlights flow.
* **Nonce**: none. Replay protection is provided at the transport
  layer (the GFS only routes within an active WS), and the moment
  envelope's ``moment_id`` is unique per author.

The recipient's verification is in
``socialhome/services/moment_public_inbound.py:_verify`` — single
``json.dumps(..., separators=(",",":"), sort_keys=True)`` call,
identical bytes-for-bytes to the sender's canonical encoding.

## DB schema (this PR ships into `0001_initial.sql`)

* `gfs_user_registrations` (GFS) — directory of opted-in users.
  Carries `bio` (≤280 chars) and `picture_digest` so the public
  directory landing renders cards without joining
  `gfs_user_pictures`.
* `gfs_user_pictures` (GFS) — avatar bytes mirrored from the home
  instance so the anon `/moments` SPA + `/moments/{id}` detail page
  can serve `<img src="…/picture?v=<digest>">` without round-
  tripping to the (often NAT-shielded) household.
* `gfs_moment_follows` (GFS) — follower graph keyed by
  `(follower_user_id, followed_user_id)`.
* `moment_public_registrations` (SH) — author-side opt-in per
  `(user_id, gfs_id)`, plus the `default_share` flag and
  `last_picture_digest` so the profile-sync flow skips redundant
  avatar uploads.
* `moment_public_follows` (SH) — follower-side cache, including the
  followed user's `home_instance_pk` for signature verification.
* `moments.is_public` / `moments.received_via` /
  `moments.received_via_gfs_id` — provenance on every row so the
  inbox UI can render a "via {gfs}" chip and so the federation
  outbound can enforce the no-redistribute rule.

## Public directory + profile sync

* **Anon landing** at `/moments` (SPA shell) and `/moments/{user_id}`
  (per-user detail) — both rendered by the GFS itself; the JS at
  `/static/users_directory.js` fetches `GET /gfs/users` and renders
  cards with avatar + name + bio + handle.
* **Per-user detail JSON** at `GET /gfs/users/{user_id}` — adds
  `follower_count` to the registration shape so the detail page
  can show social proof.
* **Server-side filter** `GET /gfs/users?q=<substr>` matches
  `display_name` and `username` (`LIKE '%q%'`). Capped at 200
  rows; the SH-side proxy passes `q` through.
* **In-app Discover** at `/momentum/public/discover` — fetches
  `GET /api/gfs/{gfs_id}/users` (SH proxy), rendering avatar via
  `GET /api/gfs/{gfs_id}/users/{user_id}/picture` so the UI
  renders even when the home instance is NAT-shielded.
* **Profile sync** — `UserProfileUpdated` (published from
  `UserService.patch_profile` and `set_picture`) is consumed by
  `ProfileSyncService`, which calls
  `MomentPublicService.push_profile_to_gfs` once per active
  registration. Failures log + drop; reconcile happens on the
  next save.
* **Single identity** — there is no per-Momentum profile override
  in v1. What's in `users.{display_name, bio, picture_hash}` is
  what every paired GFS sees.

## Implementation pointers

* SH author orchestration:
  `socialhome/services/moment_public_service.py`,
  `socialhome/services/moment_public_outbound.py`.
* SH recipient:
  `socialhome/services/moment_public_inbound.py`.
* GFS broker:
  `socialhome/global_server/moment_public_registry.py`.
* GFS routes:
  `socialhome/global_server/routes/moments_public.py`.
* SH routes:
  `socialhome/routes/moments_public.py`.
* Relay guard:
  `socialhome/services/moment_federation_outbound.py:relay_inbound`.

## Spec refs

* §Momentum (the underlying pillar; this page extends it).
* §24.7 GFS pairing handshake.
* §24.12 SH↔GFS WebSocket transport.
* §25.8.21 Encryption-first invariant.
