# Momentum

Household-broadcast posts pillar — a *moment* is a one-shot post
(text + optional image / ≤ 15-second video) that fans out to every
paired peer **and their paired peers, up to 3 hops**. Replies are
themselves moments and carry a ``parent_moment_id`` that pins them
to a thread root.

## Scope

- **HFS**: full participant. Authors post moments, peers receive and
  re-broadcast through a hop-counted relay (max 3 hops).
- **GFS**: uninvolved. Moments are personal-scope.

## Event types

`MOMENT_CREATED`, `MOMENT_DELETED`, `MOMENT_REACTED`,
`MOMENT_REACTION_REMOVED`.

Plaintext fields on every envelope: `event_type`, `from_instance`,
`to_instance`, `moment_id`, `origin_instance_id`, `hop_count`. Routing
data only — everything else (`content`, `media_url`, `media_type`,
`duration_ms`, `parent_moment_id`, `expires_at`, `author_user_id`,
`reactor_user_id`, `emoji`) lives inside the encrypted payload
(§25.8.21).

## Retention

* Absolute on-disk cap: **7 days** (``moments.expires_at`` is
  ``created_at + 7 days``).
* Visibility cap: **24 hours** for moments where the local viewer
  doesn't follow the author. ``moment_repo.list_visible_to`` collapses
  the absolute cap to 24 h via a ``user_follows`` lookup against the
  viewer's row.
* The retention scheduler runs hourly and deletes anything past the
  absolute cap; reactions cascade.

## Rate limit

One **top-level** moment per author per 15 minutes.
``moment_repo.count_recent_for_author`` ignores rows where
``parent_moment_id IS NOT NULL`` so back-and-forth replies are
unconstrained. Reactions are exempt.

## 3-hop relay

```
              hop=1                 hop=2                 hop=3
A (author)  ────►  B (paired)  ────►  C (B's paired)  ────►  D (C's paired)
                                       (skips A)              (skips A and B)
```

* The author's instance fans the envelope with
  ``hop_count = 1`` and ``origin_instance_id = self``.
* Receivers persist (PRIMARY KEY on ``moments.id`` makes the second
  receipt a no-op) and republish ``MomentCreated`` on the local bus
  for the realtime layer.
* The inbound handler then calls
  ``MomentFederationOutbound.relay_inbound``, which bumps
  ``hop_count`` and re-fans to every paired peer **except**
  ``origin_instance_id`` and the immediate ``from_instance``.
* When ``hop_count >= MOMENT_MAX_HOPS`` (3), the relay short-circuits.

The receive-side dedupe and the explicit exclusion list together
prevent fan-out loops that could form in a fully-connected mesh.

## Authority

* **1-hop direct** (``from_instance == origin_instance_id``): the
  sending peer must be the author's home instance. Inbound rejects on
  mismatch.
* **2/3-hop relay** (``from_instance != origin_instance_id``): trust
  the ``origin_instance_id`` field as long as it matches the author's
  home instance lookup. Unknown authors (``USER_UPDATED`` envelope
  hasn't landed yet) are accepted on first sight.

## Mermaid sequence — local author posts a moment

```mermaid
sequenceDiagram
    autonumber
    participant U as Author<br/>(HFS A)
    participant SA as MomentService<br/>(A)
    participant Bus as EventBus
    participant Fed as FederationService<br/>(A)
    participant B as Peer HFS B
    participant SB as MomentService<br/>(B)
    participant C as Peer HFS C<br/>(B's peer)

    U->>SA: POST /api/moments<br/>{content, media?, parent_moment_id?}
    SA->>SA: 15-min rate-limit check<br/>+ persist + ``moments.id`` UPSERT
    SA->>Bus: publish MomentCreated
    Bus->>Fed: outbound subscriber<br/>fans hop=1 to paired peers
    Fed->>B: encrypted MOMENT_CREATED (hop=1, origin=A)
    Note over B: §24.11 pipeline:<br/>verify, decrypt, persist
    B->>SB: dispatch via _event_registry<br/>save + republish MomentCreated
    SB-->>Fed: relay_inbound → fan hop=2 to B's peers (skip A)
    Fed->>C: encrypted MOMENT_CREATED (hop=2, origin=A)
    Note over C: persist + republish<br/>(no further relay; hop_count==2 + relay path → hop=3)
```

## Notifications

``NotificationService`` subscribes to the moment + follow bus
events and writes a :class:`Notification` row to the recipient's
bell when:

| Event                          | Recipient                | Type                |
|--------------------------------|--------------------------|---------------------|
| ``MomentReactionChanged``      | The moment author        | ``moment_reacted``  |
| ``MomentCreated`` (with parent)| The parent moment author | ``moment_replied``  |
| ``UserFollowed``               | The followed user        | ``user_followed``   |

Cleared reactions (``emoji is None``) don't fire a new
notification — the original reaction was the signal. Self-
reactions, self-replies, and self-follows are silent. Recipients
who live on a peer instance get the notification on *their*
instance after the federation event lands, not on the actor's
instance.

## Block + report

* **Block.** The viewer's personal block list (``user_blocks``) is
  consulted by ``moment_repo.list_visible_to`` so a blocked author's
  moments never surface — same plumbing the Stories pillar uses.
* **Report.** ``POST /api/moments/{id}/report`` files a
  ``content_reports`` row with ``target_type='moment'``. The same
  table also accepts ``target_type='story'`` (filed via
  ``POST /api/stories/{id}/report``) so admin triage runs through one
  queue at ``/api/admin/reports?status=pending``.

## Implementation pointers

- Schema: `socialhome/migrations/0001_initial.sql` — `moments`,
  `moment_reactions`, `user_follows`. ``moments.author_user_id`` is
  plain text (no FK) so federated remote-author rows live alongside
  local rows.
- Domain: `socialhome/domain/moment.py` (caps + dataclasses);
  events at `socialhome/domain/events.py`.
- Repo: `socialhome/repositories/moment_repo.py`.
- Service: `socialhome/services/moment_service.py`.
- Outbound federation:
  ``socialhome/services/moment_federation_outbound.py``.
- Inbound federation: handler block in
  ``socialhome/services/federation_inbound_service.py`` (registered
  by ``attach_to`` for the four ``MOMENT_*`` event types). Calls
  ``moment_outbound.relay_inbound`` for the 3-hop relay step.
- Realtime push:
  ``socialhome/services/realtime_service.py`` — broadcasts
  ``moment.created`` / ``moment.deleted`` to the household and
  ``moment.reaction_changed`` only to the author's WS sessions.
- Routes: `socialhome/routes/moments.py`.
- Retention scheduler:
  `socialhome/infrastructure/moment_retention_scheduler.py`.
- Frontend: `client/src/features/momentum/`,
  `client/src/store/blocks.ts` (re-used).

## Spec refs

- §24.11 inbound validation pipeline (encryption-first applies).
- §25.8.21 every field encrypted unless required for routing.
- §Momentum (this page) for the retention + relay model.
