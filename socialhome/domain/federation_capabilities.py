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
#: * **v4** — §11 peer-pairing bootstrap moves off the dedicated
#:   ``/api/pairing/peer-{accept,confirm}`` routes onto the federation
#:   inbox URL as :data:`FederationEventType.PAIRING_PEER_ACCEPT` /
#:   :data:`FederationEventType.PAIRING_PEER_CONFIRM`. Required because
#:   HA / HAOS deployments only proxy the federation inbox path —
#:   remote peers cannot reach any other SH path through Supervisor
#:   Ingress. **No fallback.** Sub-v_4 peers cannot pair under HAOS
#:   today (the bug this version fixes); standalone-to-standalone
#:   pairs with one upgraded side and one sub-v_4 side will fail too
#:   and need both sides upgraded.
#: * **v5** — :data:`FederationEventType.LOCAL_HOME_LOCATION_CHANGED`.
#:   Senders use it to push their HA-sourced home coordinates to every
#:   confirmed peer on every change. Sub-v_5 receivers reject the
#:   unknown event_type with 400 (the §24.11 pipeline's standard
#:   "Unknown event_type" path), which our outbox now treats as a
#:   PERMANENT failure and drops cleanly — so the safety net is the
#:   outbox-drop fix from PR #354. No user-visible regression for
#:   sub-v_5 peers; they just don't get the map update.
#: * **v6** — cross-instance space-invite redeem + federation mesh
#:   routing. Two related additions shipping under the same
#:   release:
#:
#:   * :data:`FederationEventType.SPACE_INVITE_TOKEN_REDEEM` family
#:     (``_REDEEM`` / ``_REDEEM_ACK`` / ``_REDEEM_DENY``). Receiver-
#:     initiated cross-instance redeem for ``socialhome://invite#…``
#:     codes — a paste on the receiver's instance routes to the
#:     issuer over federation and seats the receiver as a remote
#:     space member.
#:   * Generic mesh-routing envelope :data:`FederationEventType
#:     .SPACE_ROUTED` (PR 2) — wraps any inner event so it can
#:     traverse a chain of confirmed peers to reach a target the
#:     origin isn't directly paired with. Same envelope shape works
#:     for invite redemption, space posts, and any future event
#:     type without inventing new ``_ROUTED`` variants per case.
#:   * Route-discovery primitives :data:`FederationEventType
#:     .SPACE_FIND_ROUTE` + :data:`SPACE_ROUTE_FOUND` (PR 2). Per-
#:     target probe + response so the origin can pick a hop-count-
#:     minimal path through the federation network (default
#:     ``max_hops=3``, configurable per deployment).
#:
#:   **No fallback.** Sub-v_6 issuers can't process the redeem;
#:   the receiver-side coordinator gates the outbound on
#:   ``peer_supports(min_version=6)`` and 422s the SPA with a clear
#:   "issuer needs to upgrade" message rather than wasting the 10 s
#:   timeout window. Same gate applies to the mesh-routing
#:   envelopes once PR 2 lands.
#: * **v9** — :data:`FederationEventType.SPACE_REMOTE_ADMIN_KICK`
#:   shipped (#114 phase 2, PR #435). A remote admin can now actually
#:   kick a member of a space hosted elsewhere — host validates the
#:   actor's role from ``space_remote_members.role`` before
#:   dispatching the kick. **Force-upgrade.** Sub-v_9 hosts silently
#:   drop the command (no handler registered), so the kick appears
#:   to succeed on the actor's side but the host never applies it —
#:   the admin's intent is lost. Operators must upgrade hosts before
#:   members try cross-household admin actions.
#: * **v8** — :data:`FederationEventType.SPACE_MEMBER_ROLE_CHANGED`
#:   shipped (#114, PR #434). Host emits this every time an owner
#:   promotes / demotes a remote member's role; receivers update
#:   their local view of the roster (``space_members.role`` for the
#:   affected user's own household, ``space_remote_members.role``
#:   on witnesses). Sub-v_8 peers silently drop the event — their
#:   member-list SPA stays at the pre-change role until the user
#:   re-runs §25.6 sync or accepts a fresh invite. **Best effort.**
#:   Forward-compatible because the SPA never depends on a role
#:   it didn't see propagate; the worst-case is a stale badge.
#: * **v7** — :data:`FederationEventType.SPACE_KEY_EXCHANGE_REKEY`
#:   is now actually shipped (#121, PR #432). Every member-removal
#:   path on the host — local kick, ban, §D1b cross-household kick —
#:   rotates the space epoch and ships the new AES-256 content key
#:   to every remaining member household so a removed member can't
#:   keep decrypting future content with their cached at-rest key.
#:   The event_type itself was declared on the enum since v_1, but
#:   no sender emitted it and no receiver handled it. Sub-v_7
#:   receivers silently ignore the new outbound (event-type
#:   dispatch is best-effort — see
#:   :class:`EventDispatchRegistry.dispatch`); the resulting
#:   degradation is that they fail to decrypt every subsequent
#:   ``SPACE_POST_CREATED`` from this host until they upgrade and
#:   re-sync. **No fallback** — gracefully ignoring the rekey is a
#:   forward-secrecy violation, so we'd rather fail loud. Operators
#:   should expect to upgrade member households together with the
#:   host.
OURS: int = 9


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

    #: Minimum proto_version where the receiver knows
    #: :data:`FederationEventType.LOCAL_HOME_LOCATION_CHANGED`.
    #: Senders gate the broadcast on this; pre-v_5 peers are skipped
    #: silently (they'll learn the coords on next re-pair or via the
    #: peer-accept body if the operator unpairs / re-pairs).
    MIN_FOR_HOME_LOCATION_BROADCAST = 5

    #: Minimum proto_version where the issuer knows the
    #: ``SPACE_INVITE_TOKEN_REDEEM`` family + the multi-hop
    #: ``_ROUTED`` variants + ``SPACE_FIND_ROUTE`` route discovery.
    #: No fallback — the coordinator 422s the SPA on sub-v_6 issuers
    #: rather than waste the 10 s timeout. The receiver-side guard
    #: also doubles as protection against pasting a code minted by
    #: an instance that's been downgraded since.
    MIN_FOR_SPACE_INVITE_REDEEM = 6

    #: Minimum proto_version where the receiver registers a handler
    #: for :data:`FederationEventType.SPACE_KEY_EXCHANGE_REKEY`. Sub-
    #: v_7 peers silently drop the new event and stay on their cached
    #: epoch key, which means they fail-decrypt every subsequent post
    #: encrypted under the rotated epoch. The host emits unconditionally
    #: — gating on this constant would silently revert forward
    #: secrecy.
    MIN_FOR_SPACE_KEY_REKEY = 7

    #: Minimum proto_version where the receiver knows
    #: :data:`FederationEventType.SPACE_MEMBER_ROLE_CHANGED`. Sub-v_8
    #: peers silently drop the event and keep showing the
    #: pre-change role until a §25.6 sync refresh. Best-effort —
    #: a stale role badge is benign.
    MIN_FOR_REMOTE_MEMBER_ROLE = 8

    #: Minimum proto_version where the host knows
    #: :data:`FederationEventType.SPACE_REMOTE_ADMIN_KICK`. Sub-v_9
    #: hosts silently drop the kick command; the actor sees their UI
    #: succeed but the host never applies the change. Operators must
    #: upgrade hosts together with members.
    MIN_FOR_REMOTE_ADMIN_KICK = 9

    # v_4 (§11 pairing-via-inbox) intentionally has no named constant
    # here. Capability exchange happens *after* pairing completes, so
    # there is no point in the codepath where ``peer_supports(...,
    # min_version=4)`` could change behaviour — the sender always
    # emits the new shape, and v_3 receivers can't pair at all
    # (the legacy ``/api/pairing/peer-{accept,confirm}`` routes are
    # gone). The bump in :data:`OURS` plus the version-history entry
    # in the module docstring is the entire public surface.
