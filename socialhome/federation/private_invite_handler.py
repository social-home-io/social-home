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
from ..services.space_service import stub_space_from_metadata

if TYPE_CHECKING:
    from ..domain.federation import FederationEvent
    from ..repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo
    from .federation_service import FederationService

log = logging.getLogger(__name__)


class PrivateSpaceInviteHandler:
    """Inbound dispatcher for the :data:`SPACE_PRIVATE_INVITE*` family."""

    __slots__ = ("_bus", "_space_repo", "_remote_members")

    def __init__(
        self,
        *,
        bus: EventBus,
        space_repo: "AbstractSpaceRepo",
        remote_member_repo: "AbstractSpaceRemoteMemberRepo",
    ) -> None:
        self._bus = bus
        self._space_repo = space_repo
        self._remote_members = remote_member_repo

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
