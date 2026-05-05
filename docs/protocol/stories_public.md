# Stories — public sharing via GFS

A story author can opt a single story into public sharing through a
paired Global Federation Server (GFS). The GFS mints a revocable URL
the author can paste into Twitter / email / SMS; anyone visiting the
URL gets a public landing page. The page bootstraps a WebRTC
DataChannel **directly to the author's instance** — story bytes never
transit GFS. GFS only relays SDP/ICE during the handshake.

This is the only path in Social Home where a non-Social-Home browser
can view a story. The author's existing audience and retention rules
still apply: a publication can never outlive `stories.expires_at`, and
revoking a token (or unpublishing) takes effect on the next request.

## Scope

- **HFS** runs :class:`StoryPublicationService` (publish / revoke /
  unpublish) and (PR2) :class:`StorySignalingHandler` for the WebRTC
  answerer side.
- **GFS** runs :class:`StoryPublicationRegistry` and serves the
  ``/story/{instance}/{story}/{token}`` public landing page. PR2 adds
  the public ``/gfs/story_rtc/*`` signalling endpoints.

## URL shape

```
https://{gfs_host}/story/{instance_id}/{story_id}/{token}
```

* ``instance_id`` — author's HFS instance id.
* ``story_id`` — opaque story id on that instance.
* ``token`` — 32-byte urlsafe-base64. Revocable, multiple per
  publication. Authoritative; the GFS resolves it against
  ``gfs_story_tokens`` and rejects mismatched ``(instance_id, story_id)``
  tuples in the URL.

## Wire endpoints

Author SH → GFS (Ed25519-signed body, same `_rtc_authenticate`
middleware as `/gfs/rtc/*`):

| Method | Path | Purpose |
|---|---|---|
| POST | `/gfs/stories/{story_id}/publish` | Record a publication and mint the first share token. Body: `{story_id, instance_id, expires_at, label?, signature}`. Returns `201 {token, url, label}`. |
| POST | `/gfs/stories/{story_id}/tokens` | Mint another token under an existing publication. Body: `{label?, signature}`. Returns `201 {token, url, label}`. |
| POST | `/gfs/story_tokens/{token}/revoke` | Revoke a single token. Body must come from the publishing instance — guards against cross-instance revoke. |
| POST | `/gfs/stories/{story_id}/unpublish` | Drop the publication; CASCADE revokes every token under it. |

Public (no signature):

| Method | Path | Purpose |
|---|---|---|
| GET | `/story/{instance_id}/{story_id}/{token}` | SSR landing page. `200` (token active + author online), `410 Gone` (token revoked / publication missing / story expired / URL mismatch), `503 Unavailable` (author offline). |

PR2 will add `/gfs/story_rtc/{offer,answer-poll,ice}` for the
public-viewer signalling and `/gfs/stories/ice-servers` for the
browser bootstrap.

## Retention

* Per-publication: `gfs_story_publications.expires_at` is a unix
  epoch mirroring the author's `stories.expires_at`. The GFS cron
  call `prune_expired(now)` drops past-cap rows; `lookup_active`
  filters live too.
* Per-token: `revoked_at` is `NULL` while active; setting it to a
  unix epoch makes `resolve_token` return `None` immediately.
* Author can revoke any individual token without affecting other
  tokens under the same publication.
* No follower / viewer extension — the same retention rule that
  governs the in-mesh viewer governs the public viewer.

## Author-online check

A publication is only servable while the author's instance has a
live SH↔GFS WebSocket. The landing-page handler queries
`GfsWebSocketRegistry.is_connected(instance_id)`; offline author →
503 with a 10-second auto-refresh meta tag. PR2 needs the live WS
anyway because it pushes the public viewer's WebRTC offer over it.

## Implementation pointers

- Schema (SH side): `socialhome/migrations/0001_initial.sql` —
  `stories.public_gfs_id`, `stories.public_published_at`.
- Schema (GFS side): `socialhome/global_server/migrations/0001_initial.sql`
  — `gfs_story_publications`, `gfs_story_tokens`.
- Domain: `socialhome/domain/story.py` (`Story.public_*` fields);
  `socialhome/global_server/domain.py` (`GfsStoryPublication`,
  `GfsStoryToken`).
- Repos: `socialhome/repositories/story_repo.py`
  (`mark_published` / `mark_unpublished` / `list_published_for`);
  `socialhome/global_server/repositories.py`
  (`SqliteGfsStoryPublicationRepo`, `SqliteGfsStoryTokenRepo`).
- Services: `socialhome/services/story_publication_service.py` (SH);
  `socialhome/global_server/story_publications.py` (GFS registry).
- Routes: `socialhome/routes/story_publications.py` (SH);
  `socialhome/global_server/routes/stories.py` (GFS).

## Security notes

- Encryption-first carve-out: this is the one Social Home surface
  where content is intentionally readable without a household
  identity. The carve-out is per-story and author-initiated; nothing
  becomes public without the author flipping the toggle.
- Signed publish: every `/gfs/stories/*` mutation is Ed25519-signed
  by the author's instance and verified against the GFS-side
  `client_instances.public_key`. A bad actor cannot publish someone
  else's story.
- Token-vs-URL match: the landing handler rejects URLs whose
  `(instance_id, story_id)` segment doesn't agree with the resolved
  token, so a stolen token cannot be used to enumerate other
  stories on the same instance.
- Per-IP rate-limit on the public landing path uses the existing
  `build_listing_rate_limit()` middleware (30/min/IP).
- Author can pull every token instantly via `unpublish`. There's no
  revoke-key-rotation step — the row's deletion is the revoke.
