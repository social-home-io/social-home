"""Zero-leak cross-household invites for private spaces (§D1b).

Three inbound event types + one outbound:

* ``SPACE_PRIVATE_INVITE`` — host → invitee's household. The plaintext
  envelope carries ONLY routing fields; space metadata (space_id,
  display hint, inviter, invite_token) lives entirely in the encrypted
  payload. §25.8.21 compliant.
* ``SPACE_PRIVATE_INVITE_ACCEPT`` — invitee → host.
* ``SPACE_PRIVATE_INVITE_DECLINE`` — invitee → host.
* ``SPACE_REMOTE_MEMBER_REMOVED`` — host → former invitee's household
  when a remote member is removed from the space.

The handler persists the invitation on receive so the UI can surface
accept / decline buttons, and on accept wires the invitee as a
:class:`SpaceRemoteMember` so the host's subsequent
``SPACE_POST_CREATED`` fan-outs include them in the recipient list.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import (
    RemoteSpaceInviteAccepted,
    RemoteSpaceInviteDeclined,
    RemoteSpaceInviteReceived,
    RemoteSpaceMemberRemoved,
)
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus
from ..repositories.space_remote_location_repo import SpaceRemoteLocation
from ..services.space_service import (
    apply_space_content_key_from_metadata,
    apply_space_cover_from_metadata,
    stub_space_from_metadata,
)

if TYPE_CHECKING:
    from ..domain.federation import FederationEvent
    from ..repositories.space_cover_repo import AbstractSpaceCoverRepo
    from ..repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo
    from .federation_service import FederationService

log = logging.getLogger(__name__)


class PrivateSpaceInviteHandler:
    """Inbound dispatcher for the :data:`SPACE_PRIVATE_INVITE*` family."""

    __slots__ = (
        "_bus",
        "_space_repo",
        "_remote_members",
        "_cover_repo",
        "_space_crypto",
        "_space_service",
        "_remote_locations",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        space_repo: "AbstractSpaceRepo",
        remote_member_repo: "AbstractSpaceRemoteMemberRepo",
        cover_repo: "AbstractSpaceCoverRepo | None" = None,
        space_crypto_service=None,
        space_service=None,
        remote_location_repo=None,
    ) -> None:
        self._bus = bus
        self._space_repo = space_repo
        self._remote_members = remote_member_repo
        #: Optional — when wired, the joiner persists the host's
        #: cover bytes from ``space_meta.cover_webp_base64`` (§D1b
        #: #116) so the stub's card renders the real image.
        self._cover_repo = cover_repo
        #: Optional — when wired, the joiner imports the host's
        #: space content key from ``space_meta.space_content_key``
        #: so subsequent SPACE_POST_CREATED decrypts succeed
        #: (§D1b #117).
        self._space_crypto = space_crypto_service
        #: Optional — when wired, used to dispatch validated
        #: ``SPACE_REMOTE_ADMIN_KICK`` events through the full kick
        #: path (rotation, role-check, broadcast). Tests that don't
        #: exercise admin actions can omit it.
        self._space_service = space_service
        #: Optional — when wired, inbound ``SPACE_LOCATION_UPDATED``
        #: events from member households are persisted here so the
        #: space map endpoint surfaces remote members' pins alongside
        #: local presence.
        self._remote_locations = remote_location_repo

    def attach_space_service(self, space_service) -> None:
        """Wire :class:`SpaceService` post-construction (#114 phase 2).

        :class:`SpaceService` is built downstream of this handler, so
        the kick dispatch path needs an after-the-fact handle. Without
        it the inbound ``SPACE_REMOTE_ADMIN_KICK`` is logged + dropped
        — degrading to a no-op rather than crashing the receiver.
        """
        self._space_service = space_service

    def attach_to(self, federation_service: "FederationService") -> None:
        registry = federation_service._event_registry  # noqa: SLF001
        registry.register(
            FederationEventType.SPACE_PRIVATE_INVITE,
            self._on_invite,
        )
        registry.register(
            FederationEventType.SPACE_PRIVATE_INVITE_ACCEPT,
            self._on_accept,
        )
        registry.register(
            FederationEventType.SPACE_PRIVATE_INVITE_DECLINE,
            self._on_decline,
        )
        registry.register(
            FederationEventType.SPACE_REMOTE_MEMBER_REMOVED,
            self._on_member_removed,
        )
        registry.register(
            FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
            self._on_key_exchange_rekey,
        )
        registry.register(
            FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
            self._on_role_changed,
        )
        registry.register(
            FederationEventType.SPACE_REMOTE_ADMIN_KICK,
            self._on_remote_admin_kick,
        )
        registry.register(
            FederationEventType.SPACE_REMOTE_ADMIN_ACTION,
            self._on_remote_admin_action,
        )
        registry.register(
            FederationEventType.SPACE_LOCATION_UPDATED,
            self._on_space_location_updated,
        )

    # ── Receive ─────────────────────────────────────────────────────────

    async def _on_invite(self, event: "FederationEvent") -> None:
        """A peer invited one of our users to their private space."""
        p = event.payload
        # All fields are in the encrypted payload — envelope plaintext
        # is strictly routing metadata. §25.8.21.
        space_id = str(p.get("space_id") or "")
        invite_token = str(p.get("invite_token") or "")
        invitee_user_id = str(p.get("invitee_user_id") or "")
        if not space_id or not invite_token or not invitee_user_id:
            log.debug(
                "SPACE_PRIVATE_INVITE from %s missing required fields",
                event.from_instance,
            )
            return
        inviter_user_id = str(p.get("inviter_user_id") or "")
        display_hint = p.get("space_display_hint")
        await self._space_repo.save_remote_invitation(
            space_id=space_id,
            invited_by=inviter_user_id,
            remote_instance_id=event.from_instance,
            remote_user_id=invitee_user_id,
            invite_token=invite_token,
            space_display_hint=(str(display_hint) if display_hint else None),
        )
        # §D1b — seat a *local stub* of the host's space so accept can
        # immediately insert a ``space_members`` row pointing at it.
        # The stub is invisible to the user's /api/spaces list (which
        # joins on ``space_members``) until they accept; declining
        # leaves it dust-but-harmless. ``space.save`` is upsert so a
        # repeat invite, or a refresh after SPACE_CONFIG_CHANGED, is
        # idempotent. Older senders that don't ship ``space_meta`` get
        # the legacy behaviour — no stub, joiner sees the invite banner
        # but can't see the space until upstream upgrades.
        meta = p.get("space_meta")
        if isinstance(meta, dict):
            stub = stub_space_from_metadata(
                space_id,
                host_instance_id=event.from_instance,
                meta=meta,
            )
            await self._space_repo.save(stub)
            # §D1b cover bytes (#116) — when shipped inline, persist
            # so the stub renders the real cover rather than the
            # gradient placeholder.
            await apply_space_cover_from_metadata(
                space_id,
                meta=meta,
                cover_repo=self._cover_repo,
            )
            # §D1b space content key (#117) — persist receiver's
            # local epoch key so this invitee can actually decrypt
            # space events once she accepts. Without this the stub
            # is just metadata; SPACE_POST_CREATED inbound would
            # raise on decrypt and the user would see nothing.
            await apply_space_content_key_from_metadata(
                space_id,
                meta=meta,
                space_crypto_service=self._space_crypto,
            )
            # §D1b member-list mirror (#115) — the meta now carries a
            # ``roster`` of everyone in the space. Seat each entry as
            # a ``SpaceRemoteMember`` row so the joiner's local
            # ``GET /api/spaces/{id}/members`` (which merges
            # ``space_remote_members`` per PR #424) shows the full
            # household-spanning member list rather than just her
            # own row. We skip the invitee's *own* user_id — that
            # comes in via ``space_members`` when she accepts.
            roster = meta.get("roster")
            if isinstance(roster, list):
                for entry in roster:
                    if not isinstance(entry, dict):
                        continue
                    user_id = str(entry.get("user_id") or "")
                    inst_id = str(entry.get("instance_id") or "")
                    if not user_id or not inst_id or user_id == invitee_user_id:
                        continue
                    await self._remote_members.add(
                        space_id=space_id,
                        instance_id=inst_id,
                        user_id=user_id,
                        user_pk=(
                            str(entry["user_pk"]) if entry.get("user_pk") else None
                        ),
                        display_name=(
                            str(entry["display_name"])
                            if entry.get("display_name")
                            else None
                        ),
                    )
        await self._bus.publish(
            RemoteSpaceInviteReceived(
                space_id=space_id,
                inviter_user_id=inviter_user_id,
                invitee_user_id=invitee_user_id,
            )
        )

    async def _on_accept(self, event: "FederationEvent") -> None:
        """Our peer accepted the invite we sent — seat them as a
        :class:`SpaceRemoteMember`."""
        p = event.payload
        token = str(p.get("invite_token") or "")
        if not token:
            return
        invite = await self._space_repo.get_invitation_by_token(token)
        if invite is None:
            log.debug(
                "SPACE_PRIVATE_INVITE_ACCEPT: unknown token from %s",
                event.from_instance,
            )
            return
        invitee_user_id = str(p.get("invitee_user_id") or "")
        invitee_pk = p.get("invitee_public_key")
        invitee_display = p.get("invitee_display_name")
        await self._remote_members.add(
            space_id=invite["space_id"],
            instance_id=event.from_instance,
            user_id=invitee_user_id,
            user_pk=str(invitee_pk) if invitee_pk else None,
            display_name=str(invitee_display) if invitee_display else None,
        )
        # Register the accepting peer as a space *instance* member too —
        # ``broadcast_to_space_members`` queries ``space_instances``, so
        # without this row the peer's household never receives the
        # space's federation events (calendar events, posts, etc.).
        # The accepting host is the source-of-truth for membership, so
        # add the row on their side; the inviter learns about new peer
        # households via the existing ``SpaceMemberJoined`` channel.
        await self._space_repo.add_space_instance(
            invite["space_id"],
            event.from_instance,
        )
        await self._space_repo.update_invitation_status(
            invite["id"],
            "accepted",
        )
        await self._bus.publish(
            RemoteSpaceInviteAccepted(
                space_id=invite["space_id"],
                instance_id=event.from_instance,
                invitee_user_id=invitee_user_id,
            )
        )

    async def _on_decline(self, event: "FederationEvent") -> None:
        p = event.payload
        token = str(p.get("invite_token") or "")
        if not token:
            return
        invite = await self._space_repo.get_invitation_by_token(token)
        if invite is None:
            return
        await self._space_repo.update_invitation_status(
            invite["id"],
            "declined",
        )
        await self._bus.publish(
            RemoteSpaceInviteDeclined(
                space_id=invite["space_id"],
                instance_id=event.from_instance,
                invitee_user_id=str(p.get("invitee_user_id") or ""),
            )
        )

    async def _on_member_removed(self, event: "FederationEvent") -> None:
        """The host removed us (or one of our users) from a private
        space. Cleans up both possible local representations: the
        host-side ``space_remote_members`` row (no-op on the kicked
        user's own instance) AND the local stub ``spaces`` row + the
        kicked user's ``space_members`` row (no-op on the host's
        instance). One handler does both because the same event lands
        on both sides; each side recognises only its own data."""
        p = event.payload
        space_id = str(p.get("space_id") or "")
        user_id = str(p.get("user_id") or "")
        if not space_id or not user_id:
            return
        await self._remote_members.remove(
            space_id,
            event.from_instance,
            user_id,
        )
        # Drop any stored location pin too — without this, the kicked
        # member's last pin stays on the map until next reload OR a
        # future SPACE_LOCATION_UPDATED arrives (which the kick should
        # have already prevented at the sender side).
        if self._remote_locations is not None:
            await self._remote_locations.delete_for_member(
                space_id,
                event.from_instance,
                user_id,
            )
        # §D1b — clean up the joiner-side stub. If we have a local
        # ``space_members`` row for the kicked user, drop it. If the
        # stub's only member was that user, mark the stub dissolved
        # so the space stops appearing in surfaces that join on
        # ``space_members`` AND filter on ``dissolved=0`` (which is
        # every list-for-user query in the repo). Mark-dissolved
        # mirrors what happens to locally-owned spaces when their
        # last member leaves — the row stays as audit trail; the UI
        # treats it as gone.
        await self._space_repo.delete_member(space_id, user_id)
        remaining = await self._space_repo.list_members(space_id)
        if not remaining:
            local_space = await self._space_repo.get(space_id)
            # Only dissolve a stub — never our own locally-owned space.
            if local_space is not None and (
                local_space.owner_instance_id == event.from_instance
            ):
                await self._space_repo.mark_dissolved(space_id)
        await self._bus.publish(
            RemoteSpaceMemberRemoved(
                space_id=space_id,
                instance_id=event.from_instance,
                user_id=user_id,
            )
        )

    async def _on_role_changed(self, event: "FederationEvent") -> None:
        """Host promoted / demoted a member (#114).

        Three sides see this event:

        * The promoted user's own household — updates the local
          ``space_members.role`` row so the SPA gates admin controls
          on the new role.
        * Other member households (witnesses) — updates the local
          ``space_remote_members.role`` so the rendered member list
          shows the new badge.
        * The host's own broadcast loops back to the host's
          ``broadcast_to_space_members`` set, but the host's own
          instance isn't included by construction (see
          :meth:`AbstractFederationRepo.list_member_instance_ids`).

        Idempotent — the repo set ops upsert. Cross-household admin
        commands (kick from a non-host instance) are not implemented
        yet; this event only propagates the role assignment so the
        UI can surface controls. Actual remote admin operations
        ride a separate event family in a future PR.
        """
        p = event.payload
        space_id = str(p.get("space_id") or "") or (event.space_id or "")
        user_id = str(p.get("user_id") or "")
        member_instance = str(p.get("instance_id") or "")
        role = str(p.get("role") or "")
        if not space_id or not user_id or not member_instance or not role:
            log.debug(
                "SPACE_MEMBER_ROLE_CHANGED from %s missing required fields",
                event.from_instance,
            )
            return
        if role not in ("admin", "member"):
            log.debug(
                "SPACE_MEMBER_ROLE_CHANGED unknown role %r — skipping",
                role,
            )
            return
        # We may be the affected member's own household OR a witness.
        # The local stub for this space uses ``space_members`` for our
        # own users and ``space_remote_members`` for everyone else;
        # both paths are upserts so we can update without first knowing
        # which side we're on.
        local = await self._space_repo.get_member(space_id, user_id)
        if local is not None:
            await self._space_repo.set_role(space_id, user_id, role)
        await self._remote_members.set_role(
            space_id,
            member_instance,
            user_id,
            role,
        )

    async def _on_space_location_updated(
        self,
        event: "FederationEvent",
    ) -> None:
        """Remote member's pin for the space map.

        The remote household's :class:`SpaceLocationOutbound` already
        applied the privacy-tier (gps vs zone_only) on the sender
        side; we just persist what we're told. The
        :class:`SpacePresenceView` route merges this with local
        presence so the map renders both. Without this handler the
        envelope arrived, the dispatch registry had no handler, and
        the pin silently dropped — Pascal's symptom of "space map
        only says 1 user, that's me".

        ``space_id`` must reference an existing space row (FK on
        :data:`space_remote_member_locations`); we skip silently if
        the space is unknown locally (a remote household racing the
        invite cleanup).
        """
        if self._remote_locations is None:
            log.debug(
                "SPACE_LOCATION_UPDATED: no remote_location_repo wired",
            )
            return
        p = event.payload
        space_id = str(p.get("space_id") or "") or getattr(
            event,
            "space_id",
            "",
        )
        user_id = str(p.get("user_id") or "")
        mode = str(p.get("mode") or "")
        if not space_id or not user_id or mode not in ("gps", "zone_only"):
            log.debug(
                "SPACE_LOCATION_UPDATED from %s missing required fields",
                event.from_instance,
            )
            return
        # Only persist if the sender is in fact a remote member of
        # this space — drop spoofed events from non-members.
        match = await self._remote_members.get(
            space_id,
            event.from_instance,
            user_id,
        )
        if match is None:
            log.debug(
                "SPACE_LOCATION_UPDATED: %s@%s is not a remote member of %s",
                user_id,
                event.from_instance,
                space_id,
            )
            return
        try:
            await self._remote_locations.upsert(
                SpaceRemoteLocation(
                    space_id=space_id,
                    instance_id=event.from_instance,
                    user_id=user_id,
                    mode=mode,
                    latitude=p.get("lat"),
                    longitude=p.get("lon"),
                    accuracy_m=p.get("accuracy_m"),
                    zone_id=p.get("zone_id"),
                    zone_name=p.get("zone_name"),
                    updated_at=p.get("updated_at"),
                )
            )
        except Exception:
            log.exception(
                "SPACE_LOCATION_UPDATED: upsert failed for %s@%s in %s",
                user_id,
                event.from_instance,
                space_id,
            )

    async def _on_remote_admin_kick(self, event: "FederationEvent") -> None:
        """Cross-household admin kick command (#114 phase 2).

        Receiver is the host of the space; sender is the household
        of a remote admin who wants someone removed. The handler
        delegates to :meth:`SpaceService.apply_remote_admin_kick`
        which validates the actor's role from
        ``space_remote_members.role`` before dispatching the actual
        kick. The §24.11 pipeline has already verified the
        envelope's signature; the role-check is the second gate.
        """
        if self._space_service is None:
            log.warning(
                "SPACE_REMOTE_ADMIN_KICK: no space_service wired — dropping",
            )
            return
        p = event.payload
        space_id = str(p.get("space_id") or "") or (event.space_id or "")
        actor_user_id = str(p.get("actor_user_id") or "")
        # SECURITY: bind the actor's household to the *signed* envelope —
        # never trust a payload-supplied actor_instance_id. ``from_instance``
        # is cryptographically bound to the signer (inbound_validator); a
        # payload claim is authored by that signer, so honouring it would
        # let a confirmed peer forge an action attributed to another
        # household's admin (the role lookup would match the impersonated
        # admin's row). An admin's action MUST originate from the admin's
        # own household.
        actor_instance_id = event.from_instance
        target_user_id = str(p.get("target_user_id") or "")
        if not space_id or not actor_user_id or not target_user_id:
            log.debug(
                "SPACE_REMOTE_ADMIN_KICK from %s missing required fields",
                event.from_instance,
            )
            return
        await self._space_service.apply_remote_admin_kick(
            space_id,
            actor_instance_id=actor_instance_id,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
        )

    async def _on_remote_admin_action(self, event: "FederationEvent") -> None:
        """Generic cross-household admin action (v_15+).

        Receiver is the host of the space; sender is the household of a
        remote admin who wants to run an admin-level mutation (config
        edit, ban / unban, archive / unarchive). Delegates to
        :meth:`SpaceService.apply_remote_admin_action`, which re-validates
        the actor's role from ``space_remote_members.role`` before running
        the real host method. The §24.11 pipeline has already verified the
        envelope signature; the role-check is the second gate. Generalises
        :meth:`_on_remote_admin_kick`.
        """
        if self._space_service is None:
            log.warning(
                "SPACE_REMOTE_ADMIN_ACTION: no space_service wired — dropping",
            )
            return
        p = event.payload
        space_id = str(p.get("space_id") or "") or (event.space_id or "")
        actor_user_id = str(p.get("actor_user_id") or "")
        # SECURITY: bind the actor's household to the *signed* envelope —
        # never trust a payload-supplied actor_instance_id (see
        # ``_on_remote_admin_kick``). Honouring it would let a confirmed
        # peer impersonate another household's admin.
        actor_instance_id = event.from_instance
        action = str(p.get("action") or "")
        raw_params = p.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        if not space_id or not actor_user_id or not action:
            log.debug(
                "SPACE_REMOTE_ADMIN_ACTION from %s missing required fields",
                event.from_instance,
            )
            return
        await self._space_service.apply_remote_admin_action(
            space_id,
            actor_instance_id=actor_instance_id,
            actor_user_id=actor_user_id,
            action=action,
            params=params,
        )

    async def _on_key_exchange_rekey(self, event: "FederationEvent") -> None:
        """Forward-secrecy rekey from the host (#121).

        Triggered every time the host removes a member (local kick,
        ban, or §D1b cross-household kick) — the host rotates the
        space's epoch and ships the fresh AES-256 content key to
        every remaining member household. We persist via
        :func:`apply_space_content_key_from_metadata`, which re-wraps
        the bytes under our own KEK so the new ``space_keys`` row
        matches the at-rest invariant.

        Idempotent. A repeated REKEY for the same epoch upserts (the
        bytes will match — both sides derived from the host's
        original key). If we receive a REKEY whose ``space_id`` we
        don't own a stub for, the import is a no-op for us; the
        host's broadcast set is computed off ``space_instances``, so
        our membership state agrees with theirs.
        """
        space_id = str(event.payload.get("space_id") or "")
        if not space_id:
            return
        await apply_space_content_key_from_metadata(
            space_id,
            meta=event.payload,
            space_crypto_service=self._space_crypto,
        )
