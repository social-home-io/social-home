"""Federation protocol version — sender-side gating for new fields.

Every peer carries a single integer ``proto_version`` on its
``remote_instances`` row. It is monotonically bumped each release
that adds a federation surface (a new event type, a new payload
field whose default-if-missing would be wrong, a new wire shape).
Senders gate optional fields with
``federation_service.peer_supports(instance_id, min_version=N)``
before including them, so a v1 receiver never sees a v2 field.

Adding a new federation surface:

1. Bump :data:`OURS` to the next integer.
2. Add a named constant on :class:`FederationCapability` so callers can
   reference the new version by intent (``MIN_FOR_OCCURRENCE_OVERRIDE``)
   instead of a magic number.
3. Wherever the new surface produces an outbound, gate it on
   ``peer_supports(..., min_version=FederationCapability.X)`` and pick
   a degraded fallback for older peers (or skip the send entirely if no
   safe fallback exists).
4. Update :file:`docs/protocol/capabilities.md` with what v_N adds and
   what an older peer should fall back to.

Adding a *flag set* on top of this (per-peer feature opt-in / opt-out)
is deliberately deferred — it only earns its complexity once we have
selective deployments, forks, or asymmetric send/receive support, none
of which exist in v1. A monotonic version covers every case we have
today; flags can layer on later as a backward-compatible
``ALTER TABLE remote_instances ADD COLUMN features TEXT`` if real
operational evidence forces it.

The ``proto_version`` integer is exchanged through
:data:`FederationEventType.INSTANCE_CAPABILITIES_UPDATED` at startup
(see :class:`CapabilitiesOutbound`); the receiving peer's row picks up
the new value, and future ``peer_supports`` calls return ``True`` for
versions at or below it.
"""

from __future__ import annotations


#: The protocol version this build advertises to peers. Bump on every
#: release that ships a federation surface that older peers cannot
#: parse fail-soft (or whose missing-default would silently produce a
#: wrong-but-not-crashing state).
#:
#: History:
#:
#: * **v1** — initial wire (every event type up to the calendar-tz fix).
#: * **v2** — events may carry an IANA ``tz`` field. Old (v1) peers
#:   tolerate it because the receiver defaults ``tz`` to ``"UTC"``,
#:   so the bump is informational; future v3+ features that aren't
#:   fail-soft will be the first to actually flip behaviour via
#:   :func:`FederationService.peer_supports`.
#: * **v3** — DM media (image / video / file). ``DM_MESSAGE`` payloads
#:   may now carry ``file_name``, ``mime_type``, ``file_size_bytes``,
#:   ``media_blob_id``, and (for cross-household sends) a tiny
#:   ``preview_bytes_b64`` thumbnail / poster / glyph that the
#:   receiver renders immediately while a follow-up
#:   :data:`FederationEventType.DM_MEDIA_BLOB` ships the full bytes.
#:   Issue #319 paragraph 5 policy: **fallback**. Sub-v_3 peers
#:   receive a synthesised ``type='text'`` message — see
#:   :mod:`socialhome.federation.compat.dm_media_v3` for the
#:   transform.
OURS: int = 3


class FederationCapability:
    """Named ``proto_version`` thresholds for sender-side gating.

    Callers reference these constants instead of magic numbers so the
    intent of each gate is searchable. Adding a feature: append a new
    constant whose value equals the ``proto_version`` that introduced
    it, and document it in ``docs/protocol/capabilities.md``.
    """

    #: Minimum proto_version where event payloads carry ``tz``. Senders
    #: include the field unconditionally (the receiver defaults to UTC
    #: at any version), so this constant is informational — kept as a
    #: worked example of how the next feature should be wired.
    MIN_FOR_CALENDAR_TZ = 2

    #: Minimum proto_version where ``DM_MESSAGE`` may carry the media-
    #: attachment fields (``file_name`` / ``mime_type`` /
    #: ``file_size_bytes`` / ``media_blob_id``) AND where the receiver
    #: knows how to handle the follow-up ``DM_MEDIA_BLOB`` event.
    #: Sub-v_3 peers fall back to a synthesised ``type='text'`` message
    #: ("📎 cat.jpg — sender needs to upgrade…") so the bubble still
    #: carries useful information instead of vanishing — see the
    #: :mod:`socialhome.federation.compat.dm_media_v3` transform.
    MIN_FOR_DM_MEDIA_SYNC = 3
