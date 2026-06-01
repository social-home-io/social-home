# Database schema

Reference for the v1 SQLite schema. Every table that ships in v1 is
created by `socialhome/migrations/0001_initial.sql` (1,717 lines).
This doc groups those tables by domain so a contributor can find what
they need without reading the SQL top-to-bottom.

The migration file is the **source of truth** for column types,
indexes, and foreign keys; this page exists to navigate it. When
anything below contradicts the file, the file wins.

## Conventions

- TEXT columns hold ISO-8601 UTC timestamps unless otherwise noted.
- `instance_id` and `user_id` are 32-character base32 strings derived
  from an Ed25519 public key (§4.1.2 / §4.1.3) and never reassigned.
- Foreign keys reference public ids (string keys), never integer
  surrogates, so federation events can carry the same key across
  instance boundaries.
- JSON columns are stored as TEXT and validated at the service layer.
- GPS coordinates are stored already-truncated to 4 decimal places
  (§4 / [`principles.md`](./principles.md)).
- Schema migrations after v1 follow the `0002_*.sql` pattern; the v1
  file collapses what the spec called "migrations 0001–0033".

## Identity, users, auth

| Table | Purpose |
|---|---|
| `instance_identity` | Single-row table (`id='self'`) with the HFS's long-term Ed25519 keypair, optional ML-DSA-65 PQ key, `home_lat` / `home_lon` (4dp-truncated, nullable — populated by the HA / HAOS adapter on startup from HA Core's `/api/config`; standalone mode leaves them NULL unless the operator configures a location), and routing secret. |
| `instance_config` | Generic key/value store for instance-level settings the platform adapter or services need to persist. |
| `users` | Local household users — username, display name, profile picture hash, admin flag, theme, status, locale, child-protection fields, soft-delete state, `last_seen_at` (most recent WS disconnect — drives "Last seen X" rendering after a server restart, see `docs/protocol/presence.md` § Online status), `source` (`manual` vs `ha`), `external_id` (provider-side stable id scoped by `source` — the 32-hex HA `user_id` for `source='ha'`; lets the picture lifter / future presence bridge join via `person.attributes.user_id` without re-resolving the username, and keeps SH identity stable across HA display-name renames), and `tz` (the user's personal IANA timezone, auto-detected from the browser on first login via a `PATCH /api/me` probe; falls back to the household tz when still `'UTC'`). |
| `user_profile_pictures` | WebP bytes for household-level profile pictures, keyed by `user_id`. Separate table so `SELECT * FROM users` stays cheap. |
| `remote_users` | Users on paired remote instances. Carries display name, alias, picture hash, public key, `deprovisioned_at`, and `synced_at`. Same `user_id` namespace as `users`. |
| `api_tokens` | Per-user API tokens (HA mode and integrations). Stores `token_hash` only — the plaintext is shown to the user once. |
| `platform_users` | Standalone-mode local accounts (`platform/standalone/`). Empty in HA mode. Stores password hash, email, notify endpoint. |
| `platform_tokens` | Bearer tokens for `platform_users`. Hash-only storage. |
| `password_reset_tokens` | Admin-issued, single-use, 1h-TTL tokens that let a user set a new password. Stores SHA-256 of the raw token; `used_at` flips on consume so a token can't be replayed. |
| `auth_audit_log` | Append-only trail of password-bearing auth events: `login_success`, `login_failure`, `reset_issue`, `reset_redeem_success`, `reset_redeem_failure`. Each row carries `username` (NULL when the request didn't carry a recoverable principal), `ip_address`, and free-form `metadata` JSON. Read by admins via `GET /api/admin/auth-audit`. |

## Federation: peers, pairing, outbox, replay

| Table | Purpose |
|---|---|
| `remote_instances` | One row per paired peer. Holds the peer's `instance_id`, identity public key, KEK-encrypted directional session keys, inbox URL, status (`pending_*` / `confirmed` / `unpairing`), source (`manual` / `space_session`), `proto_version` (monotonic int the peer advertised via `INSTANCE_CAPABILITIES_UPDATED` at startup — drives sender-side gating; see `docs/protocol/capabilities.md`), and the negotiated `sig_suite` (`ed25519` / `ed25519+mldsa65`). `home_lat` / `home_lon` (4dp-truncated, nullable) hold the peer's household coordinates — populated from the `PAIRING_PEER_ACCEPT` body on first contact or updated by inbound `LOCAL_HOME_LOCATION_CHANGED` events (capability v5+); used by the Connections Map tab. The `local_alias` column (added in migration `0005`) holds the admin's local-only rename of the peer; `NULL` means "fall back to `display_name`". Never federated — purely local UX state surfaced in Friends / Connections / DM views. The `share_home` column (added in migration `0006`, `INTEGER NOT NULL DEFAULT 1`) is a per-pair flag controlling whether this household's home coordinates are shared with the peer: `1` (default) — coordinates are included in the outbound `LOCAL_HOME_LOCATION_CHANGED` fan-out; `0` — the peer is skipped during fan-out, and flipping to `0` immediately fires a null-coord revoke envelope. Never federated — local-only policy state. |
| `pending_pairings` | In-flight QR pairings. Stores own DH keypair, peer DH/identity material once received, `verification_code` (SAS), inbox URL, status, and expiry. KEK-encrypted private DH until pairing confirms. |
| `pairing_relay` | Admin-pending `PAIRING_INTRO_RELAY` requests received from a paired peer (§11.9). |
| `federation_outbox` | Pending outbound envelopes — `event_type`, encrypted `payload_json`, `attempts`, `next_attempt_at`, `status`. `expires_at = NULL` for security-critical events; 7-day TTL for ordinary ones (§4.4.7). |
| `federation_replay_cache` | `msg_id` → `received_at` for inbound dedup. Pruned by `infrastructure/replay_cache_scheduler.py`. |
| `network_discovery` | Peer-graph cache for §11.10 BFS path-finding — one row per `(instance_id, discovered_via)` edge. Compound PK so multiple paths to a peer are independent rows. |
| `gfs_connections` | Paired Global Federation Servers — `gfs_instance_id`, public key, inbox URL, status. |
| `gfs_space_publications` | Which public spaces are currently published to which GFS connection. |

## Household feed and content

| Table | Purpose |
|---|---|
| `preferences` | Polymorphic preferences table. `id='household'` holds the single household-wide row (feature toggles `feat_feed`, `feat_pages`, `feat_tasks`, `feat_stickies`, `feat_calendar`, `feat_bazaar`, `feat_presence`, `feat_gallery` — all defaulting to 1 — plus `household_name` and `tz`). A row with `id=<user_id>` holds per-user sidebar preferences (`hide_highlights`, `hide_momentum`, `hide_bazaar` — all defaulting to 0). The Python scope policy (`PREFERENCE_SCOPE` in `socialhome/domain/preferences.py`) is the authoritative list of which columns belong to which scope. The `tz` column on the household row is the IANA wall-clock anchor (`'UTC'` at install; mirrored from HA Core's `time_zone` on adapter startup in `ha` / `haos` mode; admin-editable in `standalone` mode). Calendar events created without an explicit `tz` resolve through this column at the floor of the fallback chain. |
| `feed_posts` | Household-level posts. Type enum covers `text`, `image`, `video`, `transcript`, `poll`, `schedule`, `file`, `bazaar`. Holds reactions JSON, comment count, pinned, deleted, edited_at. |
| `post_comments` | Threaded household post comments with `parent_id` self-reference. |
| `saved_posts` / `feed_read_positions` | Per-user saved posts and last-read marker. |
| `post_drafts` | Auto-saved post composer drafts. `context` is `household_feed`, a `space_id`, a `page_id`, or a `conv_id`. GC'd by `post_draft_scheduler`. |
| `polls` / `poll_options` / `poll_votes` | Household-feed polls. `polls` is keyed by the parent `feed_posts.id`. Votes are stored per `(option_id, voter_user_id)`. |
| `schedule_slots` / `schedule_responses` / `schedule_poll_meta` | Household-feed Doodle-style schedule polls. |
| `bazaar_listings` / `bazaar_bids` / `bazaar_offers` | Household-feed marketplace: fixed-price, offer, bid-from, negotiable, and auction modes. Bids vs offers are distinct concepts. |
| `saved_bazaar_listings` | Per-user saved listings. |
| `shopping_list_items` | Single shared household shopping list (§23.120). Each item optionally carries a free-form `store` name (auto-upserts a `shopping_stores` row on first sighting) so the SPA can render the list grouped by shop in trip order. |
| `shopping_stores` | Household's store catalogue + drag-defined "trip order". One row per `name` (PK), with `sort_order` driving the section order in the grouped shopping view. Auto-populated from `shopping_list_items.store`; rows are NOT removed when their last referencing item is deleted so the household keeps its order across empty lists. |
| `household_theme` | Single-row (`id='default'`) household theme settings — primary/accent colour, surface, mode (`light`/`dark`/`auto`), font, density, corner radius. |
| `stickies` | Household + space sticky notes. `space_id IS NULL` means household-level. |

## Spaces

| Table | Purpose |
|---|---|
| `spaces` | Core space row — id (derived from space identity public key), name, owner, `space_type` (`private`/`household`/`public`/`global`), `join_mode`, retention, feature toggles (per-tab: calendar/todo/location/stickies/pages/gallery/bazaar), the per-post-type allow-list (`allow_post_*` — which post kinds members may compose in the feed; federates over `space_meta.features.allowed_post_types`), posts/pages/stickies/calendar/tasks access modes, public-discovery fields (lat/lon/radius), cover hash, `bot_enabled`, `min_age`, `target_audience`, `dissolved`, `archived` (soft, reversible read-only archive — distinct from `dissolved` which is hard-gone; federates over `space_meta`), `welcome_version`, and `tz` (the space's IANA wall-clock anchor for calendar events; defaults to the household tz at create time, editable by space admins for federated multi-household spaces that pin to a different city). |
| `space_members` | Local membership: `(space_id, user_id, role, joined_at, history_visible_from, location_share_enabled, space_display_name, picture_hash)`. Roles are `owner` / `admin` / `member` / `subscriber` (use the `SpaceRole` enum, not bare strings). |
| `space_member_profile_pictures` | Per-space profile-picture override bytes, keyed by `(space_id, user_id)`. |
| `space_remote_members` | Cross-household members admitted via `SPACE_PRIVATE_INVITE` (§D1b). Lets fan-out include the invitee's instance. Carries a per-row `role` (`'member'` or `'admin'`, never `'owner'`) from migration `0009_space_remote_members_role.sql`; promotions flow over `SPACE_MEMBER_ROLE_CHANGED` (#114). |
| `space_zones` | Per-space labelled circles for the space map (§23.8.7). 4dp lat/lon, 25–50000 m radius. Replicated to remote member instances via sealed `SPACE_ZONE_*` events. |
| `space_covers` | Hero-banner WebP bytes per space. |
| `space_instances` | One row per `(space_id, instance_id)` — which peer instances participate in a space, with `last_seen_at`. |
| `space_keys` | One row per `(space_id, epoch)` holding the KEK-encrypted AES-256 content key. Epoch advances on member removal/ban (§4.3). |
| `space_bans` / `space_instance_bans` | Banned users / banned remote instances. Identity public key is captured for cross-instance ban enforcement. |
| `space_admin_proposals` / `space_admin_proposal_votes` | Multi-admin approval (quorum) for critical actions — dissolve + publication-tier change (`0016_space_admin_proposals.sql`, v_16). Authoritative on the host; a read-only mirror on member households (kept in sync by `SPACE_ADMIN_PROPOSAL_UPDATED`) so remote admins can render the pending proposal + tally and vote. Both reference `spaces(id) ON DELETE CASCADE`. A mirror row also persists the host's authoritative SPA view (`host_view_json`, `0017_space_proposal_mirror_view.sql`) so a member household shows the host's exact tally instead of recomputing it from its own roster. |
| `space_invitations` | Local + cross-household invites. Invitee-side and host-side rows both exist for `type=private` cross-household invites (§D1b). |
| `space_invite_tokens` | Shareable invite-link tokens — `uses_remaining`, optional expiry. |
| `space_join_requests` | Open join requests for `request`-mode and global spaces. Captures local + cross-household applicants (§D2). |
| `space_aliases` | Per-space-and-local-user aliases — what label the **space** sees for a local user. Federated. |
| `user_aliases` | Per-viewer **private** aliases (§4.1.6). Resolution priority: `space_display_name` > personal alias > global display name. Never federated. |
| `pinned_sidebar_spaces` | Per-user space pin-order in the sidebar. |
| `space_themes` / `space_links` / `space_notif_prefs` | Per-space theme overrides, link tray, and per-user notification preferences. |
| `peer_space_directory` | Cached "From friends" tab — type=public spaces hosted by paired peers (§D1a). One row per `(instance_id, space_id)`; replaced atomically per peer on `SPACE_DIRECTORY_SYNC`. |
| `space_bots` | Named bot personas attached to a space (§bot-bridge). Scope is `space` (admin-curated) or `member` (member-created). `token_hash` is sha256 of the bearer token. |

### Space content

| Table | Purpose |
|---|---|
| `space_posts` | Space-level posts. Mirrors `feed_posts` shape; adds `bot_id` (NULL for human authors), `linked_event_id` (set when the post is the auto-created surface for a calendar event in Phase B), and `hidden_from_feed` (1 when the post is an entity's anchor — a Bazaar listing's wrapper post — that lives in its own tab and was not announced in the feed; §23.15). |
| `space_post_comments` | Threaded comments on `space_posts`. |
| `space_moderation_queue` | Pending mod actions — feature, action, payload, reviewer, status, expiry. |
| `space_polls` / `space_poll_options` / `space_poll_votes` | Space-feed polls. Vote rows are encrypted in transit so the GFS never sees who voted what (§25.8.21). |
| `space_schedule_slots` / `space_schedule_responses` / `space_schedule_poll_meta` | Space-scoped Doodle polls. |
| `space_calendar_events` | Space calendar events. Holds RFC 5545 `rrule`, attendees JSON, `capacity`, `notify_before_minutes`, `location` (free-form), `announce_in_feed` (§23.15 — 1 mirrors the event to the space feed as a `PostType.EVENT` post via `CalendarFeedBridge`; defaults 0 so events live only in the Calendar tab unless the creator opts in), and `tz` — the event's IANA wall-clock anchor (`'UTC'` default; resolved at create time from explicit request → `spaces.tz` → household `tz`). Recurrence expansion and reminder fire-at computation honour `tz` so a "weekly 19:00 Europe/Berlin" event stays at 19:00 Berlin across DST transitions. |
| `space_calendar_rsvps` | Per-occurrence RSVPs — `(event_id, user_id, occurrence_at)` PK. Status: `going` / `maybe` / `declined` / `requested` / `waitlist`. |
| `space_calendar_rsvp_reminders` | Pre-event reminder fan-out — `fire_at` partial index on un-sent + future. Driven by `infrastructure/calendar_reminder_scheduler.py`. |
| `space_calendar_feed_tokens` | Per-`(user, space)` revocable tokens for the iCal `.ics` feed. The `token_hash` column holds a SHA-256 hash of the raw token (matching `api_tokens`); a leaked DB never exposes a live feed URL. Separate from API tokens so revoking one doesn't affect the other. |
| `pending_federated_rsvps` | Buffer for RSVP federation events arriving before their event has propagated. Flushed on event arrival. |
| `space_pages` | Wiki-style space pages with edit-lock fields and pending-delete approval workflow. |
| `space_page_snapshots` | Concurrent-edit conflict resolution (§4.4.4.1). Sides are `base` / `mine` / `theirs`; `conflict=1` blocks further edits until resolved. |
| `space_task_lists` / `space_tasks` | Space task lists and tasks; mirror household `task_lists` / `tasks` with `space_id`. |

## Direct messages

| Table | Purpose |
|---|---|
| `conversations` | DM threads. Type is `dm` or `group_dm`. `bot_enabled` opts the conversation into the integration's bot bridge. |
| `conversation_members` / `conversation_remote_members` | Local + remote membership rows; `history_visible_from` controls how far back a new joiner can see. |
| `conversation_messages` | Per-message rows. Plaintext storage (DM federation is transport-only encrypted; the local DB stores plaintext like every other surface — see [`principles.md`](./principles.md)). |
| `message_reactions` | One row per `(message_id, user_id, emoji)`. |
| `conversation_message_gaps` | Detected gaps in `(conversation, sender)` sequence — drives the gap-fill request. |
| `conversation_relay_paths` | Multi-hop DM relay paths (§12.5). `path_index=0` is primary; alternatives promote on relay-offline fallback. `relay_path` is a JSON array of instance ids. |
| `conversation_sender_sequences` | Per-`(conversation, sender)` last-emitted sequence number, used to detect gaps and order out-of-order delivery. |
| `dm_relay_seen` | Dedup table for `DM_RELAY` envelopes (§12.5.3). Pruned by the DM GC scheduler. |
| `conversation_delivery_state` | Per-`(message, user)` delivery + read state. |
| `dm_contact_requests` | Pending DM contact requests (§23.47). |
| `conversation_messages.reply_to_highlight_frame_id` | Highlight-frame reply target (§Highlights). No FK so the message survives retention deletion of the source frame. |
| `conversation_messages.reply_to_highlight_frame_snapshot` | JSON snapshot frozen at reply-time: `{thumb_url, author_user_id, highlight_date, caption_text?, caption_emoji?}`. Keeps the reply readable after the source frame is purged. |
| `conversation_messages.file_name` / `mime_type` / `file_size_bytes` | Media-attachment metadata for `type` ∈ {`image`, `video`, `file`}. `mime_type` drives the SPA's render branch (inline `<img>` / `<video>` / file-pill). NULL on non-media or pre-v_3 rows. See [DM media](./protocol/dm-media.md). |
| `conversation_messages.media_blob_id` | Cross-household correlation id between the v_3 `DM_MESSAGE` (carrying the small preview) and the follow-up `DM_MEDIA_BLOB` event (carrying the full bytes). NULL for same-household DMs and non-media messages. |
| `conversation_messages.media_sync_status` | `'pending'` while the receiver is still waiting for the full-bytes blob; flips to NULL once the blob lands and `media_url` points at local bytes. `'failed'` after the sender's outbox exhausts its retry budget. NULL on legacy / non-media rows. |
| `dm_media_outbox` | Per-(blob_id × target_instance) queue of pending `DM_MEDIA_BLOB` sends. The scheduler (lands in a follow-up PR) reads `status='pending'` rows due now, encrypts the file at `bytes_path` under the conversation key, ships, and deletes on confirmation. Exponential-backoff retries via `attempts` + `next_attempt_at`; gives up after the configured retry cap and flips to `failed`. |

## Highlights

Personal "highlights" pillar — a per-author per-day frame bag that
federates to peers based on the author's audience kind. Retention is
author-controlled via `users.preferences_json["highlights"]` and the
in-process `HighlightRetentionScheduler`.

| Table | Purpose |
|---|---|
| `highlights` | One row per author per day (`UNIQUE(author_user_id, highlight_date)`). Carries `audience_kind`, an `audience_json` allow-list, and an `expires_at` cutoff set to `created_at + retention_days`. `public_gfs_id` (FK SET NULL → `gfs_connections.id`) and `public_published_at` flip when the author opts the highlight into public sharing via a GFS (§highlights_public). |
| `highlight_frames` | Image / short-video frames keyed by `(highlight_id, sequence)`. `frame_type` ∈ `image \| video`; `media_url` is the canonical `/api/media/{filename}` path. |
| `highlight_frame_views` | Per-`(frame, viewer)` view record. Drives the "viewed by N" UX surfaced on the author's viewer. |
| `highlight_frame_reactions` | Per-`(frame, reactor)` quick-reaction. Single row per viewer per frame; UPSERT to change. |
| `feed_posts.linked_highlight_id` | When `type = 'highlight_share'`, points at `highlights.id`. ON DELETE SET NULL — the share-card flips to a "Highlight has ended" placeholder when retention purges the source. |
| `space_posts.linked_highlight_id` | Same pointer for `space_posts`; FK-free since the linked highlight may live on a remote instance. |

GFS-side tables (in `socialhome/global_server/migrations/0001_initial.sql`):

| Table | Purpose |
|---|---|
| `gfs_highlight_publications` | One row per `(highlight_id, instance_id)` opted into public sharing. `expires_at` mirrors the author's retention so a publication can never outlive the highlight it advertises. `publish_signature` caches the Ed25519 over the publish body for audit. |
| `gfs_highlight_tokens` | Revocable share-link tokens under a publication (composite FK CASCADE on the publication PK). `revoked_at` is `NULL` while active; ``label`` is the author-supplied "for-twitter" hint. |

## Momentum

Household-broadcast posts that fan to a 3-hop peer mesh. Replies are
themselves moments and link to the conversation root via
`parent_moment_id`.

| Table | Purpose |
|---|---|
| `moments` | One row per moment. `author_user_id` is plaintext (no FK) so federated remote-author rows live alongside local rows. `expires_at` is the absolute 7-day cap; `list_visible_to` collapses to 24 h for non-followers. `hop_count` (default 1) records the federation hop the row landed on locally — viewers' per-user ``max_hops`` preference (§Momentum-relay-policy) compares against this column. `is_public` / `received_via` / `received_via_gfs_id` carry the §Momentum-public provenance. |
| `moment_reactions` | Per-`(moment, reactor)` emoji. PK on `(moment_id, reactor_user_id)`, UPSERT to change. Cascades on moment delete. |
| `moment_hashtags` | Per-`(moment, tag)` index of ASCII `#tag` tokens extracted from `moments.content` at save time. PK on `(moment_id, tag)`; secondary index on `(tag, moment_id)` powers the trending and tag-filter queries. Cascades on moment delete. |
| `user_follows` | Voluntary adult-to-adult follow. PK on `(follower_user_id, followed_user_id)`. Used by `list_visible_to` to extend the visibility window from 24 h to 7 d for the followed author's moments. |
| `household_instance_bans` | Operator-managed list of remote instances banned at the household level (§Momentum-relay-policy). Inbound envelopes from a banned instance are dropped at ingress; the relay-out path also skips banned sources. Distinct from per-user `user_blocks`, which stay private to the social layer. |
| `moment_public_registrations` | Per-`(user_id, gfs_id)` opt-in to fan moments through a GFS (§Momentum-public). `default_share` flips whether the composer pre-checks "Public via GFS"; `last_picture_digest` is the avatar hash last successfully pushed to that GFS so the profile-sync flow can skip redundant uploads. |

## Notifications and push

| Table | Purpose |
|---|---|
| `notifications` | In-app notification feed — id, user_id, type, title, optional body (omitted for DMs / location messages / UGC per §25.3), link URL, read_at. |
| `push_subscriptions` | Web Push endpoints — `endpoint`, `p256dh`, `auth_secret` (sensitive). One row per browser/device. |

## Presence

| Table | Purpose |
|---|---|
| `presence` | Local users' presence — `home` / `zone` / `away` / `unavailable`, current zone name, 4dp lat/lon, GPS accuracy. |
| `remote_presence` | Same shape, sourced from `PRESENCE_UPDATED` federation events. FK-free on the source columns so it works before `USERS_SYNC` populates `users`. |

## Calls

| Table | Purpose |
|---|---|
| `call_sessions` | One row per call — type (`audio` / `video`), status (`ringing` / `active` / `ended` / `declined` / `missed`), participant list JSON, started/connected/ended timestamps, duration. |
| `call_quality_samples` | Per-peer ~10 s WebRTC stats samples (RTT, jitter, loss, audio/video bitrate). Used for admin diagnostics (§26). |

## Public discovery and moderation

| Table | Purpose |
|---|---|
| `public_space_cache` | Local cache of GFS-published public spaces — `name`, `description`, lat/lon/radius, member count, `min_age`, `target_audience`. |
| `blocked_discover_instances` | Operator block-list applied to the discovery directory — never show spaces from these instances. |
| `hidden_public_spaces` | Per-user hide list for the discovery view. |
| `content_reports` | User-filed reports on posts / comments / users / spaces. Categories: `spam` / `harassment` / `inappropriate` / `misinformation` / `other`. (Note: the migration file declares this table three times to absorb earlier drift; only the final declaration is authoritative.) |
| `user_blocks` | Per-user block list (`blocker_user_id`, `blocked_user_id`). |

## Tasks (household)

| Table | Purpose |
|---|---|
| `task_lists` / `tasks` | Household task lists and tasks. Tasks have `assignees_json`, status (`todo` / `in_progress` / `done`), `rrule` for recurrence, `last_spawned_at`, `recurrence_parent_id`, `archived_at`. |
| `task_deadline_notifications` | Dedup table for fired deadline notifications, keyed by `(task_id, due_date)`. |
| `task_comments` / `task_attachments` | Comments and file attachments on tasks (§23.68). |

## Calendar (household)

| Table | Purpose |
|---|---|
| `calendars` | Personal + space calendars. Personal calendars are owned by a username; space calendars share lifecycle with their space. |
| `calendar_events` | Personal calendar events with `rrule`, attendees, `mirrored_from` (when a space event is mirrored into a personal calendar). New columns: `origin` ∈ `{local, remote_invite}` distinguishes locally-authored rows from cross-household invite mirrors; `remote_event_id` + `remote_instance_id` link a mirror back to the organiser's row so RSVP responses propagate via `PERSONAL_CALENDAR_RSVP_UPDATED`; `location` carries free-form venue/address text emitted as the iCal `LOCATION:` line on export; `tz` is the event's IANA wall-clock anchor (`'UTC'` default; resolved at create time from explicit request → `users.tz` → household `tz`) so the host's intended local time is preserved across DST and viewer-side timezone differences; `client_event_uuid` is a nullable client-stamped UUID shared across every row of a multi-attendee fan-out (one POST per picked household calendar) so the SPA's agenda groups the rows back into one card by intent rather than the content heuristic (issue #327). |
| `calendar_event_rsvps` | Personal-calendar RSVPs — cross-household invites only. PK `(event_id, user_id, occurrence_at)`. Status ∈ `{accepted, declined, tentative}`. Local household members never RSVP — the household is the unit of trust and members coordinate by writing directly to each other's calendars via the dialog's calendar selector. |

## Pages (household)

| Table | Purpose |
|---|---|
| `pages` | Household-level wiki pages — title, content, cover image, lock-by/at/expiry, pending-delete approval. |
| `page_edit_history` | Append-only history of page edits. Unique by `(page_id, version)`. |

## Gallery (§23.119)

| Table | Purpose |
|---|---|
| `gallery_albums` | Album shells. `space_id IS NULL` for household-level albums. `retention_exempt` opts the album out of space retention sweeps. `is_system` marks the auto-managed "Posts" album that mirrors every photo and video shared via the feed (one per scope, enforced by a partial unique index on `COALESCE(space_id, '__household__')`); `owner_user_id` is `NULL` for that row. |
| `gallery_items` | Album items — type (`photo` / `video`), filename + thumbnail filename, dimensions, duration, caption, taken_at, sort order. `source_post_id` is set when the row was mirrored from a feed post (drives O(1) cleanup on post-delete via `idx_gallery_items_source_post`); `NULL` for direct user uploads. |

## Child protection

| Table | Purpose |
|---|---|
| `cp_guardians` | Guardian → minor links granting view/control rights (§CP). |
| `cp_minor_blocks` | Per-minor block list applied by guardians. |
| `minor_space_memberships_audit` | Append-only audit of minor join/leave/block events on spaces. |
| `guardian_audit_log` | Append-only audit of guardian actions on minors. |

## Search

| Table | Purpose |
|---|---|
| `search_index` (FTS5 virtual) | Unified contentless full-text index across posts, space posts, DM messages, and pages. `scope` discriminates the source table; `ref_id` is the source-row id. Tokenizer is `unicode61 remove_diacritics 2`. |

## Schema source

- **File**: `socialhome/migrations/0001_initial.sql`
- **Spec**: §28 (migrations) and §29 (schema reference).

When adding or changing a table after v1, drop a `0002_*.sql` (or
later) into `socialhome/migrations/` and update both the matching
section above and the `Sqlite*Repo` that owns it. See `CLAUDE.md` →
**"Keep docs in sync"** for the reviewer rule.

## Migrations after v1

| File | Purpose |
|---|---|
| `0002_calendar_timezone.sql` | Adds `tz TEXT NOT NULL DEFAULT 'UTC'` to `household_features` (the predecessor of `preferences`), `users`, `spaces`, `calendar_events`, `space_calendar_events`. Anchors every calendar event to an IANA wall-clock zone so recurrence expansion stays DST-correct and cross-household viewing can render both the host's wall clock and the viewer's local equivalent. Pre-existing rows backfill to `'UTC'`; the service layer resolves the right value at create time. |
| `0004_calendar_client_event_uuid.sql` | Adds nullable `client_event_uuid TEXT` to `calendar_events`. The SPA composer mints one v4 UUID before a multi-target fan-out and stamps it on every POST in the batch so the resulting rows can be grouped back into one agenda card by intent (issue #327). Pre-existing rows stay NULL; the SPA's content-key fallback in `groupSharedEvents` covers them. A partial index on the column powers cross-household lookups when the federation envelope arrives with a uuid. |
| `0006_share_home.sql` | Adds `share_home INTEGER NOT NULL DEFAULT 1` to `remote_instances`. Controls per-pair home-coordinate sharing: `1` (default) includes the peer in `LOCAL_HOME_LOCATION_CHANGED` fan-out; `0` excludes it and fires a one-shot null-coord revoke envelope on the flip. Pre-existing confirmed pairs backfill to `1` (sharing on, matching prior behaviour). See `docs/protocol/home-location.md` — Revoking access. |
| `0007_preferences_rename.sql` | Renames `household_features` to `preferences` and extends it into a polymorphic table. Adds `feat_presence`, `feat_gallery` (default 1), `hide_highlights`, `hide_momentum`, `hide_bazaar` (default 0), and the `id` primary key is repurposed to hold `'household'` for the household row and a literal `user_id` for per-user rows. The old `feat_highlights` / `feat_momentum` household-gate columns are dropped — those features are now always enabled at the household level and can only be hidden per-user via the new `hide_*` columns. |
