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
  wire shape.
