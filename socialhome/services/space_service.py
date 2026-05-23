"""Space service — spaces, membership, invites, join requests, space posts.

Covers the core space lifecycle a v1 household needs:

* Create and dissolve a space (owner only).
* Update space name / features / join-mode / retention (owner or admin)
  with an atomic ``config_sequence`` bump for federation ordering.
* Member management — add / remove / set-role / list, plus bans.
* Invites (create token, accept), join requests (open→approve/deny).
* Space posts — create, edit, delete, reactions, comments. Access-level
  routing (open / moderated / admin_only) runs through the moderation
  queue for non-admin members.

Permissions enforced here (route layer never duplicates them):

* ``_require_member(space_id, user_id)`` for any read or member-level
  mutation.
* ``_require_admin_or_owner`` for config updates, bans, invites.
* ``_require_owner`` for dissolve + ownership transfer.

Polls, tasks, pages and calendar events on a space are delegated to their
own sibling services. The space-posts code here deliberately stops short
of them.
"""

from __future__ import annotations

import base64
import logging
import unicodedata
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

from ..crypto import generate_identity_keypair

if TYPE_CHECKING:
    from ..federation.route_discovery import RouteDiscoveryService
    from ..federation.routed_envelope import SpaceRoutedHandler
from ..domain.events import (
    CommentAdded,
    CommentDeleted,
    CommentUpdated,
    PostDeleted,
    PostEdited,
    RemoteJoinRequestApproved,
    SpaceConfigChanged,
    SpaceLocationFeatureEnabled,
    SpaceLocationModeChanged,
    SpaceJoinApproved,
    SpaceJoinDenied,
    SpaceJoinRequested,
    SpaceMemberJoined,
    SpaceMemberLeft,
    SpaceMemberProfileUpdated,
    SpaceModerationApproved,
    SpaceModerationQueued,
    SpaceModerationRejected,
    SpacePostCreated,
    SpacePostModerated,
)
from ..domain.federation import FederationEventType, PairingStatus
from ..media.image_processor import ImageProcessor
from ..repositories.profile_picture_repo import compute_picture_hash
from ..domain.post import (
    FEED_POST_MAX_IMAGES,
    Comment,
    CommentType,
    FileMeta,
    LocationData,
    Post,
    PostType,
)
from ..domain.presence import truncate_coord
from ..domain.space import (
    JoinMode,
    ModerationAlreadyDecidedError,
    ModerationStatus,
    PublicSpaceLimitError,
    Space,
    SpaceConfigEventType,
    SpaceFeatures,
    SpaceMember,
    SpaceModerationItem,
    SpacePermissionError,
    SpaceRole,
    SpaceType,
)
from ..infrastructure.event_bus import EventBus
from ..repositories.base import row_to_dict
from ..repositories.space_post_repo import AbstractSpacePostRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.user_repo import AbstractUserRepo
from ..domain.media_constraints import SPACE_COVER_MAX_DIMENSION
from ..services.user_service import PROFILE_PICTURE_MAX_DIMENSION
from .space_crypto_service import (
    KEY_SUITE_AESGCM_256,
    SUPPORTED_KEY_SUITES,
    UnsupportedKeySuite,
)


log = logging.getLogger(__name__)


#: Sentinel for ``update_member_profile`` partial-patch kwargs.
_UNSET_MEMBER_PROFILE = object()


#: Upper bound on simultaneously-advertised public spaces per instance
#: (spec §13). Enforced at ``create_space`` time for PUBLIC spaces.
MAX_PUBLIC_SPACES = 5

#: Post content caps — matches FeedService values.
MAX_POST_LENGTH = 10_000
MAX_COMMENT_LENGTH = 2_000


class SpaceService:
    """Orchestrates space lifecycle + member + post flows."""

    __slots__ = (
        "_spaces",
        "_posts",
        "_users",
        "_bus",
        "_own_instance_id",
        "_child_protection",
        "_pictures",
        "_covers",
        "_gfs",
        "_federation_repo",
        "_federation",
        "_remote_members",
        "_redeem_coordinator",
        "_space_crypto",
    )

    def __init__(
        self,
        space_repo: AbstractSpaceRepo,
        space_post_repo: AbstractSpacePostRepo,
        user_repo: AbstractUserRepo,
        bus: EventBus,
        *,
        own_instance_id: str,
    ) -> None:
        self._spaces = space_repo
        self._posts = space_post_repo
        self._users = user_repo
        self._bus = bus
        self._own_instance_id = own_instance_id
        self._child_protection = None
        self._pictures = None
        self._covers = None
        self._gfs = None
        self._federation_repo = None
        self._federation = None
        self._remote_members = None
        self._redeem_coordinator = None
        self._space_crypto = None

    def attach_child_protection(self, child_protection_service) -> None:
        """Wire §CP.F1 enforcement into add_member."""
        self._child_protection = child_protection_service

    def attach_profile_picture_repo(self, repo) -> None:
        """Wire the blob store so per-space picture uploads can land."""
        self._pictures = repo

    def attach_cover_repo(self, repo) -> None:
        """Wire the space-cover blob store (§23 customization)."""
        self._covers = repo

    def attach_space_crypto_service(self, space_crypto) -> None:
        """Wire SpaceContentEncryption so §D1b invite/redeem envelopes
        can ship the current epoch's space content key — the symmetric
        AES-256 secret that decrypts every event in the space. Required
        for cross-household members to read content; without it, the
        receiver's local ``space_keys`` row stays empty and every
        ``decrypt`` call against an inbound event raises."""
        self._space_crypto = space_crypto

    def attach_gfs_connection_service(self, gfs_service) -> None:
        """Wire outbound GFS publish so ``space_type=global`` spaces
        auto-advertise without a separate admin action. Optional: when
        no GFS is paired, GfsConnectionService may be absent entirely.
        """
        self._gfs = gfs_service

    def attach_federation(
        self,
        federation_service,
        federation_repo,
        remote_member_repo,
    ) -> None:
        """Wire §D1b cross-household-invite outbound. Optional: when
        federation isn't initialised yet (early boot) or tests don't
        need it, remains None and :meth:`invite_remote_user` raises.

        Also subscribes to :class:`RemoteJoinRequestApproved` so §D2
        federated join-request approvals auto-consume the invite
        token on the applicant's side.
        """
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._remote_members = remote_member_repo
        self._bus.subscribe(
            RemoteJoinRequestApproved,
            self._on_remote_join_request_approved_bus,
        )

    def attach_redeem_coordinator(self, coordinator) -> None:
        """Wire the §D2 cross-instance invite-token redeem driver.

        Optional: when not attached (early boot / unit tests),
        :meth:`redeem_invite_token` falls back to the local-only
        :meth:`accept_invite_token` path and refuses non-local
        ``issuer_instance_id`` requests.
        """
        self._redeem_coordinator = coordinator

    def attach_mesh(
        self,
        *,
        route_service: RouteDiscoveryService,
        routed_handler: SpaceRoutedHandler,
    ) -> None:
        """Wire the §D2-PR2 federation-mesh routing pair.

        Thin delegation to :meth:`FederationService.attach_mesh` —
        mesh state lives on the federation service so every space-
        content fanout (not just the private-invite family) benefits
        from per-peer mesh fallback. Kept here so existing wiring
        sites don't have to change their call site, but the source
        of truth is the federation service itself.

        Raises ``RuntimeError`` if :meth:`attach_federation` hasn't
        run yet — there's nowhere to hang the mesh refs without a
        live :class:`FederationService`.
        """
        if self._federation is None:
            raise RuntimeError(
                "space_service.attach_mesh: federation not attached; "
                "call attach_federation first",
            )
        self._federation.attach_mesh(
            route_service=route_service,
            routed_handler=routed_handler,
        )

    async def _on_remote_join_request_approved_bus(
        self,
        event: RemoteJoinRequestApproved,
    ) -> None:
        await self.on_remote_join_request_approved(
            event.request_id,
            invite_token=event.invite_token,
        )

    async def _auto_publish_on_type(
        self,
        space_id: str,
        *,
        was_global: bool,
        is_global: bool,
    ) -> None:
        """Fan publish/unpublish calls out to every active GFS when a
        space crosses the global boundary. Failures are logged inside
        :class:`GfsConnectionService`; never raised.
        """
        if self._gfs is None or was_global == is_global:
            return
        if is_global:
            await self._gfs.publish_space_to_all(space_id)
        else:
            await self._gfs.unpublish_space_from_all(space_id)

    async def set_cover(
        self,
        space_id: str,
        *,
        actor_username: str,
        raw_bytes: bytes,
    ) -> Space:
        """Transcode the upload to WebP, persist, bump cover_hash, and
        publish :class:`SpaceConfigChanged` so federation + WS fan out.
        """
        if self._covers is None:
            raise RuntimeError("cover repo not attached")
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        webp = await ImageProcessor().generate_thumbnail(
            raw_bytes,
            size=SPACE_COVER_MAX_DIMENSION,
        )
        hash_ = compute_picture_hash(webp)
        await self._covers.set(
            space_id,
            bytes_webp=webp,
            hash=hash_,
            width=SPACE_COVER_MAX_DIMENSION,
            height=SPACE_COVER_MAX_DIMENSION,
        )
        await self._spaces.set_cover_hash(space_id, hash_)
        sequence = await self._spaces.increment_config_sequence(space_id)
        updated = replace(space, cover_hash=hash_)
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=SpaceConfigEventType.COVER_UPDATED.value,
                payload={"cover_hash": hash_},
                sequence=sequence,
            )
        )
        return updated

    async def clear_cover(
        self,
        space_id: str,
        *,
        actor_username: str,
    ) -> Space:
        if self._covers is None:
            raise RuntimeError("cover repo not attached")
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        await self._covers.clear(space_id)
        await self._spaces.set_cover_hash(space_id, None)
        sequence = await self._spaces.increment_config_sequence(space_id)
        updated = replace(space, cover_hash=None)
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=SpaceConfigEventType.COVER_UPDATED.value,
                payload={"cover_hash": None},
                sequence=sequence,
            )
        )
        return updated

    # ── Space lifecycle ────────────────────────────────────────────────

    async def create_space(
        self,
        *,
        owner_username: str,
        name: str,
        description: str | None = None,
        emoji: str | None = None,
        space_type: SpaceType | str = SpaceType.PRIVATE,
        join_mode: JoinMode | str = JoinMode.INVITE_ONLY,
        features: SpaceFeatures | None = None,
        retention_days: int | None = None,
        retention_exempt_types: tuple[str, ...] | list[str] | None = None,
        lat: float | None = None,
        lon: float | None = None,
        radius_km: float | None = None,
    ) -> Space:
        """Create a new space and seat the creator as owner."""
        owner = await self._users.get(owner_username)
        if owner is None:
            raise KeyError(f"owner {owner_username!r} not found")
        if not name.strip():
            raise ValueError("space name must not be empty")

        stype = _coerce_space_type(space_type)
        jmode = _coerce_join_mode(join_mode)

        if stype is SpaceType.PUBLIC:
            count = len(await self._spaces.list_by_type(SpaceType.PUBLIC))
            if count >= MAX_PUBLIC_SPACES:
                raise PublicSpaceLimitError(
                    f"instance already advertises {count} public spaces "
                    f"(max {MAX_PUBLIC_SPACES})"
                )
            if lat is None or lon is None:
                raise ValueError("public space requires lat + lon")
        else:
            # Non-public spaces never carry location metadata.
            lat = lon = radius_km = None

        kp = generate_identity_keypair()
        exempt_types = _normalise_exempt_types(retention_exempt_types)
        space = Space(
            id=uuid.uuid4().hex,
            name=name.strip(),
            owner_instance_id=self._own_instance_id,
            owner_username=owner.username,
            identity_public_key=kp.public_key.hex(),
            config_sequence=0,
            features=features or SpaceFeatures(),
            space_type=stype,
            join_mode=jmode,
            description=description.strip() if description else None,
            emoji=emoji,
            retention_days=retention_days
            if (retention_days is None or retention_days > 0)
            else None,
            retention_exempt_types=exempt_types,
            lat=_round4(lat),
            lon=_round4(lon),
            radius_km=radius_km,
        )
        await self._spaces.save(space)
        # Seat the creator as owner.
        await self._spaces.save_member(
            SpaceMember(
                space_id=space.id,
                user_id=owner.user_id,
                role=SpaceRole.OWNER,
                joined_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        await self._spaces.add_space_instance(space.id, self._own_instance_id)
        await self._auto_publish_on_type(
            space.id,
            was_global=False,
            is_global=stype is SpaceType.GLOBAL,
        )
        return space

    async def dissolve_space(
        self,
        space_id: str,
        *,
        actor_username: str,
    ) -> None:
        """Mark a space dissolved (owner only)."""
        space = await self._require_space(space_id)
        await self._require_owner(space, actor_username)
        await self._spaces.mark_dissolved(space_id)
        await self._auto_publish_on_type(
            space_id,
            was_global=space.space_type is SpaceType.GLOBAL,
            is_global=False,
        )
        sequence = await self._spaces.increment_config_sequence(space_id)
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=SpaceConfigEventType.DISSOLVED.value,
                payload={},
                sequence=sequence,
            )
        )

    async def update_config(
        self,
        space_id: str,
        *,
        actor_username: str,
        name: str | None = None,
        description: str | None = None,
        emoji: str | None = None,
        features: SpaceFeatures | None = None,
        join_mode: JoinMode | str | None = None,
        space_type: SpaceType | str | None = None,
        retention_days: int | None = None,
        retention_exempt_types: tuple[str, ...] | list[str] | None = None,
        about_markdown: str | None | object = _UNSET_MEMBER_PROFILE,
        bot_enabled: bool | None = None,
    ) -> Space:
        """Owner or admin may update space metadata. Atomically bumps
        ``config_sequence`` and publishes :class:`SpaceConfigChanged`.

        Flipping ``space_type`` to/from ``global`` also triggers
        auto-publish/unpublish against every paired GFS
        (via :meth:`_auto_publish_on_type`).
        """
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)

        payload: dict = {}
        new_fields: dict = {}
        was_global = space.space_type is SpaceType.GLOBAL
        will_be_global = was_global
        if name is not None:
            new = name.strip()
            if not new:
                raise ValueError("space name must not be empty")
            new_fields["name"] = new
            payload["name"] = new
        if description is not None:
            new_fields["description"] = description.strip() or None
            payload["description"] = new_fields["description"]
        if emoji is not None:
            new_fields["emoji"] = emoji or None
            payload["emoji"] = new_fields["emoji"]
        location_mode_changed = False
        location_feature_just_enabled = False
        if features is not None:
            location_mode_changed = (
                features.location_mode != space.features.location_mode
            )
            # Track OFF→ON transition so we can nudge members after the write.
            location_feature_just_enabled = (
                not space.features.location and features.location
            )
            new_fields["features"] = features
            payload["features"] = features.to_wire_dict()
        if join_mode is not None:
            jmode = _coerce_join_mode(join_mode)
            new_fields["join_mode"] = jmode
            payload["join_mode"] = jmode.value
        if space_type is not None:
            stype = _coerce_space_type(space_type)
            if stype is SpaceType.PUBLIC and space.space_type is not SpaceType.PUBLIC:
                count = len(await self._spaces.list_by_type(SpaceType.PUBLIC))
                if count >= MAX_PUBLIC_SPACES:
                    raise PublicSpaceLimitError(
                        f"instance already advertises {count} public spaces "
                        f"(max {MAX_PUBLIC_SPACES})",
                    )
            new_fields["space_type"] = stype
            payload["space_type"] = stype.value
            will_be_global = stype is SpaceType.GLOBAL
        if retention_days is not None:
            # Zero or negative means "no retention limit" → None
            new_fields["retention_days"] = (
                retention_days if retention_days > 0 else None
            )
            payload["retention_days"] = new_fields["retention_days"]
        if retention_exempt_types is not None:
            exempt = _normalise_exempt_types(retention_exempt_types)
            new_fields["retention_exempt_types"] = exempt
            payload["retention_exempt_types"] = list(exempt)
        if about_markdown is not _UNSET_MEMBER_PROFILE:
            # Narrow the ``str | None | object`` sentinel to a ``str | None``
            # for mypy — once past the sentinel check, only real values remain.
            raw: str | None = about_markdown  # type: ignore[assignment]
            cleaned = (raw or "").strip() or None
            if cleaned and len(cleaned) > 8000:
                raise ValueError("about_markdown must be ≤ 8000 chars")
            new_fields["about_markdown"] = cleaned
            payload["about_markdown"] = cleaned
        if bot_enabled is not None:
            new_fields["bot_enabled"] = bool(bot_enabled)
            payload["bot_enabled"] = bool(bot_enabled)

        if not new_fields:
            return space

        updated = replace(space, **new_fields)
        await self._spaces.save(updated)
        sequence = await self._spaces.increment_config_sequence(space_id)
        if "space_type" in new_fields:
            event_type = SpaceConfigEventType.PUBLIC_MODE_CHANGED.value
        elif set(payload.keys()) == {"name"}:
            event_type = SpaceConfigEventType.RENAME.value
        else:
            event_type = SpaceConfigEventType.FEATURE_CHANGED.value
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=event_type,
                payload=payload,
                sequence=sequence,
            )
        )
        await self._auto_publish_on_type(
            space_id,
            was_global=was_global,
            is_global=will_be_global,
        )
        if location_mode_changed:
            # §23.8.6: refire latest presence so receivers see the new
            # privacy tier within seconds rather than waiting for the
            # next HA push. SpaceLocationOutbound listens.
            await self._bus.publish(
                SpaceLocationModeChanged(
                    space_id=space_id,
                    new_mode=updated.features.location_mode,
                ),
            )
        if location_feature_just_enabled:
            # Nudge members to opt in. Look up the actor's user_id so the
            # notification handler can exclude them.
            actor = await self._users.get(actor_username)
            actor_user_id = actor.user_id if actor is not None else ""
            await self._bus.publish(
                SpaceLocationFeatureEnabled(
                    space_id=space_id,
                    space_name=updated.name,
                    actor_user_id=actor_user_id,
                )
            )
        return updated

    # ── Membership ─────────────────────────────────────────────────────

    async def add_member(
        self,
        space_id: str,
        *,
        actor_username: str,
        user_id: str,
        role: str = SpaceRole.MEMBER,
    ) -> SpaceMember:
        """Add a member directly. Used for the owner-admin path and for
        accepting an invite on this instance. Regular members join via
        invite / join-request flows below.

        §CP.F1: when a :class:`ChildProtectionService` is attached, this
        path enforces the space's ``min_age`` against the user's
        ``declared_age``.
        """
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        if await self._spaces.is_banned(space_id, user_id):
            raise SpacePermissionError(
                f"user {user_id!r} is banned from this space",
                banned=True,
            )
        # §CP.F1 — block underage minors when CP is wired in.
        if self._child_protection is not None:
            await self._child_protection.check_space_age_gate(space_id, user_id)
        member = SpaceMember(
            space_id=space_id,
            user_id=user_id,
            role=role,
            joined_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._spaces.save_member(member)
        await self._bus.publish(
            SpaceMemberJoined(
                space_id=space_id,
                user_id=user_id,
                role=role,
            )
        )
        # §CP audit — record joined action for minors. No-op when the
        # user isn't CP-covered or when CP isn't wired.
        if self._child_protection is not None:
            actor = await self._users.get(actor_username)
            actor_uid = actor.user_id if actor is not None else actor_username
            await self._child_protection.record_membership_change(
                user_id=user_id,
                space_id=space_id,
                action="joined",
                actor_id=actor_uid,
            )
        return member

    async def remove_member(
        self,
        space_id: str,
        *,
        actor_username: str,
        user_id: str,
    ) -> None:
        """Remove a member. Admin/owner can remove anyone; a member can
        remove themselves.
        """
        space = await self._require_space(space_id)
        actor = await self._users.get(actor_username)
        if actor is None:
            raise KeyError(f"actor {actor_username!r} not found")
        is_self = actor.user_id == user_id
        if not is_self:
            await self._require_admin_or_owner(space, actor_username)
        target = await self._spaces.get_member(space_id, user_id)
        if target is None:
            return
        if target.role == SpaceRole.OWNER:
            raise SpacePermissionError(
                "owner cannot be removed (transfer ownership first)"
            )
        await self._spaces.delete_member(space_id, user_id)
        await self._bus.publish(
            SpaceMemberLeft(
                space_id=space_id,
                user_id=user_id,
            )
        )
        if self._child_protection is not None:
            await self._child_protection.record_membership_change(
                user_id=user_id,
                space_id=space_id,
                action="removed",
                actor_id=actor.user_id,
            )
        await self._rotate_and_distribute_space_key(space_id)

    async def set_role(
        self,
        space_id: str,
        *,
        actor_username: str,
        user_id: str,
        role: str,
    ) -> None:
        """Only the owner can promote/demote admins. Owner cannot be demoted."""
        space = await self._require_space(space_id)
        await self._require_owner(space, actor_username)
        if role == SpaceRole.OWNER:
            raise ValueError("use transfer_ownership to assign owner role")
        target = await self._spaces.get_member(space_id, user_id)
        if target is None:
            raise KeyError(f"user {user_id!r} is not a member")
        if target.role == SpaceRole.OWNER:
            raise SpacePermissionError("cannot demote the owner")
        await self._spaces.set_role(space_id, user_id, role)
        sequence = await self._spaces.increment_config_sequence(space_id)
        evt = (
            SpaceConfigEventType.ADMIN_GRANTED
            if role == SpaceRole.ADMIN
            else SpaceConfigEventType.ADMIN_REVOKED
        )
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=evt.value,
                payload={"user_id": user_id, "role": role},
                sequence=sequence,
            )
        )

    # ── Per-space profile (§4.1.6) ─────────────────────────────────────

    async def update_member_profile(
        self,
        space_id: str,
        user_id: str,
        *,
        actor_user_id: str,
        space_display_name: str | None | object = _UNSET_MEMBER_PROFILE,
    ) -> SpaceMember:
        """Patch member display-name override. Picture mutations go
        through :meth:`set_member_picture` / :meth:`clear_member_picture`.
        Only the member themselves or a space admin may patch."""
        member = await self._spaces.get_member(space_id, user_id)
        if member is None:
            raise KeyError(f"user {user_id!r} is not a member")
        await self._require_self_or_space_admin(
            space_id,
            member=member,
            actor_user_id=actor_user_id,
        )
        if space_display_name is not _UNSET_MEMBER_PROFILE:
            raw: str | None = space_display_name  # type: ignore[assignment]
            next_name = (raw.strip() if raw else None) or None
            await self._spaces.set_member_profile(
                space_id,
                user_id,
                space_display_name=next_name,
                picture_hash=member.picture_hash,
            )
            member = replace(member, space_display_name=next_name)
        await self._bus.publish(
            SpaceMemberProfileUpdated(
                space_id=space_id,
                user_id=user_id,
                space_display_name=member.space_display_name,
                picture_hash=member.picture_hash,
            )
        )
        return member

    async def set_member_picture(
        self,
        space_id: str,
        user_id: str,
        *,
        actor_user_id: str,
        raw_bytes: bytes,
    ) -> SpaceMember:
        if self._pictures is None:
            raise RuntimeError("profile picture repo not attached")
        member = await self._spaces.get_member(space_id, user_id)
        if member is None:
            raise KeyError(f"user {user_id!r} is not a member")
        await self._require_self_or_space_admin(
            space_id,
            member=member,
            actor_user_id=actor_user_id,
        )
        webp = await ImageProcessor().generate_thumbnail(
            raw_bytes,
            size=PROFILE_PICTURE_MAX_DIMENSION,
        )
        hash_ = compute_picture_hash(webp)
        await self._pictures.set_member_picture(
            space_id,
            user_id,
            bytes_webp=webp,
            hash=hash_,
            width=PROFILE_PICTURE_MAX_DIMENSION,
            height=PROFILE_PICTURE_MAX_DIMENSION,
        )
        await self._spaces.set_member_profile(
            space_id,
            user_id,
            space_display_name=member.space_display_name,
            picture_hash=hash_,
        )
        updated = replace(member, picture_hash=hash_)
        await self._bus.publish(
            SpaceMemberProfileUpdated(
                space_id=space_id,
                user_id=user_id,
                space_display_name=updated.space_display_name,
                picture_hash=hash_,
                picture_webp=webp,
            )
        )
        return updated

    async def clear_member_picture(
        self,
        space_id: str,
        user_id: str,
        *,
        actor_user_id: str,
    ) -> SpaceMember:
        if self._pictures is None:
            raise RuntimeError("profile picture repo not attached")
        member = await self._spaces.get_member(space_id, user_id)
        if member is None:
            raise KeyError(f"user {user_id!r} is not a member")
        await self._require_self_or_space_admin(
            space_id,
            member=member,
            actor_user_id=actor_user_id,
        )
        await self._pictures.clear_member_picture(space_id, user_id)
        await self._spaces.set_member_profile(
            space_id,
            user_id,
            space_display_name=member.space_display_name,
            picture_hash=None,
        )
        updated = replace(member, picture_hash=None)
        await self._bus.publish(
            SpaceMemberProfileUpdated(
                space_id=space_id,
                user_id=user_id,
                space_display_name=updated.space_display_name,
                picture_hash=None,
            )
        )
        return updated

    async def _require_self_or_space_admin(
        self,
        space_id: str,
        *,
        member: SpaceMember,
        actor_user_id: str,
    ) -> None:
        if member.user_id == actor_user_id:
            return
        actor = await self._spaces.get_member(space_id, actor_user_id)
        if actor is None or actor.role not in (SpaceRole.OWNER, SpaceRole.ADMIN):
            raise PermissionError(
                "only the member or a space admin may change this profile",
            )

    async def transfer_ownership(
        self,
        space_id: str,
        *,
        actor_username: str,
        to_user_id: str,
    ) -> None:
        space = await self._require_space(space_id)
        await self._require_owner(space, actor_username)
        new_owner_member = await self._spaces.get_member(space_id, to_user_id)
        if new_owner_member is None:
            raise KeyError(f"user {to_user_id!r} is not a member")
        # The outgoing owner becomes admin; the new owner becomes owner.
        outgoing = await self._users.get(actor_username)
        assert outgoing is not None
        await self._spaces.set_role(space_id, outgoing.user_id, SpaceRole.ADMIN)
        await self._spaces.set_role(space_id, to_user_id, SpaceRole.OWNER)
        new_owner_user = await self._users.get_by_user_id(to_user_id)
        updated = replace(
            space,
            owner_username=(
                new_owner_user.username
                if new_owner_user is not None
                else space.owner_username
            ),
        )
        await self._spaces.save(updated)
        sequence = await self._spaces.increment_config_sequence(space_id)
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=SpaceConfigEventType.OWNERSHIP_TRANSFERRED.value,
                payload={"new_owner_user_id": to_user_id},
                sequence=sequence,
            )
        )

    async def ban(
        self,
        space_id: str,
        *,
        actor_username: str,
        user_id: str,
        reason: str | None = None,
    ) -> None:
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        target = await self._spaces.get_member(space_id, user_id)
        if target is not None and target.role == SpaceRole.OWNER:
            raise SpacePermissionError("cannot ban the owner")
        actor = await self._users.get(actor_username)
        assert actor is not None
        await self._spaces.ban_member(
            space_id,
            user_id,
            banned_by=actor.user_id,
            reason=reason,
        )
        sequence = await self._spaces.increment_config_sequence(space_id)
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=SpaceConfigEventType.MEMBER_BANNED.value,
                payload={"user_id": user_id, "reason": reason},
                sequence=sequence,
            )
        )
        if self._child_protection is not None:
            await self._child_protection.record_membership_change(
                user_id=user_id,
                space_id=space_id,
                action="blocked",
                actor_id=actor.user_id,
            )
        await self._rotate_and_distribute_space_key(space_id)

    async def unban(
        self,
        space_id: str,
        *,
        actor_username: str,
        user_id: str,
    ) -> None:
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        await self._spaces.unban_member(space_id, user_id)
        sequence = await self._spaces.increment_config_sequence(space_id)
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=SpaceConfigEventType.MEMBER_UNBANNED.value,
                payload={"user_id": user_id},
                sequence=sequence,
            )
        )

    # ── Invites / join requests ────────────────────────────────────────

    async def create_invite_token(
        self,
        space_id: str,
        *,
        actor_username: str,
        uses: int = 1,
        expires_at: str | None = None,
    ) -> str:
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        actor = await self._users.get(actor_username)
        assert actor is not None
        return await self._spaces.create_invite_token(
            space_id,
            created_by=actor.user_id,
            uses=max(1, int(uses)),
            expires_at=expires_at,
        )

    async def _send_invite_envelope(
        self,
        *,
        to_instance_id: str,
        event_type: FederationEventType,
        payload: dict,
    ) -> None:
        """Ship a private-invite-family envelope to a remote instance.

        Delegates to :meth:`FederationService.send_with_mesh_fallback`
        — direct delivery for CONFIRMED peers, SPACE_ROUTED multi-hop
        for unpaired / unreachable ones, failure otherwise. Raises
        :class:`SpacePermissionError` when neither path is available
        so the route handler returns 4xx instead of 200.
        """
        if self._federation is None or self._federation_repo is None:
            raise RuntimeError("federation not attached")
        result = await self._federation.send_with_mesh_fallback(
            to_instance_id=to_instance_id,
            event_type=event_type,
            payload=payload,
        )
        if not result.ok:
            raise SpacePermissionError("no path to invitee household")

    async def _rotate_and_distribute_space_key(self, space_id: str) -> None:
        """Forward-secrecy: mint a fresh epoch + ship the new key to
        remaining members (#121).

        Called after every member-removal path — local removal, ban,
        and §D1b cross-household kick. Without rotation, a removed
        member who keeps their old at-rest key bytes would still be
        able to decrypt every future post in the space; that defeats
        the entire reason the host kicked them.

        The fan-out targets ``space_instances`` (via
        ``broadcast_to_space_members``), which the cross-household
        kick path scrubs of the removed peer right before calling
        here — so the new key naturally never lands at the kicked
        household. Local-only kicks broadcast to the remaining mesh
        of peer households; the kicked local user's own household
        re-imports the new key in-place (a no-op for them since
        they're not in ``space_members`` anymore).

        Failures are logged and swallowed — a rotation that can't
        federate is still better than no rotation, and the kick
        itself succeeded. A subsequent member action retries
        rotation; in steady state the §25.6 sync handshake will
        catch up any peer that missed the rekey.
        """
        if self._space_crypto is None or self._federation is None:
            return
        try:
            new_epoch = await self._space_crypto.rotate_epoch(space_id)
        except Exception:
            log.exception(
                "rotate_and_distribute_space_key: rotate_epoch failed for %s",
                space_id,
            )
            return
        try:
            exported = await self._space_crypto.export_current_key(space_id)
        except Exception:
            log.exception(
                "rotate_and_distribute_space_key: export_current_key failed for %s",
                space_id,
            )
            return
        if exported is None:
            return
        epoch, raw_key = exported
        if epoch != new_epoch:
            log.warning(
                "rotate_and_distribute_space_key: epoch drift for %s "
                "(rotated=%d, exported=%d)",
                space_id,
                new_epoch,
                epoch,
            )
        payload = {
            "space_id": space_id,
            "space_content_key": {
                "epoch": epoch,
                "key_suite": KEY_SUITE_AESGCM_256,
                "key_base64": base64.b64encode(raw_key).decode("ascii"),
            },
        }
        try:
            await self._federation.broadcast_to_space_members(
                space_id,
                FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
                payload,
            )
        except Exception:
            log.exception(
                "rotate_and_distribute_space_key: rekey broadcast failed for %s",
                space_id,
            )

    async def invite_remote_user(
        self,
        space_id: str,
        *,
        actor_username: str,
        invitee_instance_id: str,
        invitee_user_id: str,
    ) -> str:
        """§D1b — invite a user on another household into this space.

        Only valid when the invitee's household is a CONFIRMED peer of
        ours. Sends a zero-leak ``SPACE_PRIVATE_INVITE`` envelope (all
        space metadata rides inside the encrypted payload; see
        :data:`FederationEventType.SPACE_PRIVATE_INVITE`).
        Returns the invite token so callers can echo it in their own
        audit log.
        """
        if self._federation is None or self._federation_repo is None:
            raise RuntimeError(
                "space_service: federation not attached; "
                "remote invites require a live FederationService",
            )
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        actor = await self._users.get(actor_username)
        assert actor is not None

        # Short-TTL (5 min) single-use token minted by create_invite_token;
        # reusable with the existing POST /api/spaces/join path once the
        # invitee accepts.
        expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        token = await self._spaces.create_invite_token(
            space_id,
            created_by=actor.user_id,
            uses=1,
            expires_at=expires,
        )
        await self._spaces.save_remote_invitation(
            space_id=space_id,
            invited_by=actor.user_id,
            remote_instance_id=invitee_instance_id,
            remote_user_id=invitee_user_id,
            invite_token=token,
            space_display_hint=space.name,
        )
        # §25.8.21 — zero-leak envelope: space_id + invite_token + all
        # space metadata ride in the *encrypted* payload only. We do
        # NOT pass space_id to send_event (would put it in plaintext).
        # ``invitee_user_id`` is required so the receiving instance can
        # fan the invite out to the right local user — without it,
        # :meth:`PrivateSpaceInviteHandler._on_invite` early-returns
        # and ``GET /api/remote_invites`` stays empty on the recipient.
        await self._send_invite_envelope(
            to_instance_id=invitee_instance_id,
            event_type=FederationEventType.SPACE_PRIVATE_INVITE,
            payload={
                "space_id": space_id,
                "invite_token": token,
                "invitee_user_id": invitee_user_id,
                "inviter_user_id": actor.user_id,
                "inviter_display_name": (actor.display_name or actor.username),
                "space_display_hint": space.name,
                "expires_at": expires,
                # §D1b — the receiver needs enough metadata to seat a
                # local *stub* of this space (so it shows up in their
                # /spaces list after accept), without us shipping any
                # data the joiner couldn't already read from the host's
                # SPACE_CONFIG_CHANGED stream after pairing. Keep the
                # shape under one ``space_meta`` key so older receivers
                # ignore it cleanly via dict-get semantics — no version
                # bump needed.
                "space_meta": await build_space_snapshot_for_federation(
                    space,
                    space_repo=self._spaces,
                    remote_member_repo=self._remote_members,
                    user_repo=self._users,
                    own_instance_id=self._own_instance_id,
                    cover_repo=self._covers,
                    space_crypto_service=self._space_crypto,
                ),
            },
        )
        return token

    async def accept_remote_invite(
        self,
        *,
        token: str,
        user_id: str,
    ) -> None:
        """§D1b — invitee side: accept a cross-household private-space
        invite. Sends a SPACE_PRIVATE_INVITE_ACCEPT back to the host.
        """
        if self._federation is None:
            raise RuntimeError("federation not attached")
        invite = await self._spaces.get_invitation_by_token(token)
        if invite is None:
            raise KeyError("invite token invalid or expired")
        host_instance = invite.get("remote_instance_id")
        if not host_instance:
            raise ValueError("not a cross-household invite")
        display = None
        user_pk = None
        users_repo = self._users
        if hasattr(users_repo, "get_by_id"):
            user = await users_repo.get_by_id(user_id)
            if user is not None:
                display = user.display_name or user.username
                user_pk = getattr(user, "public_key", None)
        await self._send_invite_envelope(
            to_instance_id=host_instance,
            event_type=FederationEventType.SPACE_PRIVATE_INVITE_ACCEPT,
            payload={
                "invite_token": token,
                "invitee_user_id": user_id,
                "invitee_public_key": user_pk,
                "invitee_display_name": display,
            },
        )
        await self._spaces.update_invitation_status(
            invite["id"],
            "accepted",
        )
        # Record the (space_id, host_instance_id) mapping locally so
        # later space-scoped events the peer mints (RSVPs, comments,
        # …) federate back to the host. Without this row,
        # ``broadcast_to_space_members`` returns no targets on the
        # invitee side and the events go nowhere.
        await self._spaces.add_space_instance(
            invite["space_id"],
            host_instance,
        )
        # §D1b — seat the local membership row pointing at the stub
        # spaces row that was created when SPACE_PRIVATE_INVITE
        # arrived (see ``PrivateSpaceInviteHandler._on_invite``).
        # ``list_for_user`` JOINs on ``space_members``, so this is
        # what actually surfaces the space in the invitee's
        # ``/api/spaces``. If the stub doesn't exist (older sender,
        # no metadata shipped), the FK constraint will trip and we
        # log+skip — the invitee can still accept, just won't see
        # the space until upstream upgrades.
        stub = await self._spaces.get(invite["space_id"])
        if stub is not None:
            await self._spaces.save_member(
                SpaceMember(
                    space_id=invite["space_id"],
                    user_id=user_id,
                    role=SpaceRole.MEMBER.value,
                    joined_at=datetime.now(timezone.utc).isoformat(),
                )
            )
        else:
            log.warning(
                "accept_remote_invite: stub space row missing for"
                " space_id=%s — invitee will see the invite as accepted"
                " but the space won't show up locally until SPACE_CONFIG"
                "_CHANGED upserts the stub. Sender on older protocol?",
                invite["space_id"],
            )

    async def decline_remote_invite(
        self,
        *,
        token: str,
        user_id: str,
    ) -> None:
        if self._federation is None:
            raise RuntimeError("federation not attached")
        invite = await self._spaces.get_invitation_by_token(token)
        if invite is None:
            raise KeyError("invite token invalid or expired")
        host_instance = invite.get("remote_instance_id")
        if not host_instance:
            raise ValueError("not a cross-household invite")
        await self._send_invite_envelope(
            to_instance_id=host_instance,
            event_type=FederationEventType.SPACE_PRIVATE_INVITE_DECLINE,
            payload={
                "invite_token": token,
                "invitee_user_id": user_id,
            },
        )
        await self._spaces.update_invitation_status(
            invite["id"],
            "declined",
        )

    async def remove_remote_member(
        self,
        space_id: str,
        *,
        actor_username: str,
        instance_id: str,
        user_id: str,
    ) -> None:
        """§D1b — drop a remote member + tell their household."""
        if self._federation is None or self._remote_members is None:
            raise RuntimeError("federation not attached")
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        await self._remote_members.remove(space_id, instance_id, user_id)
        # Audit-fix (HIGH from PR #429 review): if that was the
        # last remote member from this peer instance, also drop the
        # ``space_instances`` row so subsequent broadcasts via
        # ``broadcast_to_space_members`` stop trying to deliver
        # content to the kicked household. Without this, future
        # space posts / comments / reactions keep getting shipped
        # to a household that no longer has anyone in the space —
        # transport-rule-compliant (the envelope is encrypted) but
        # wasteful, and could leak metadata about the space's
        # activity to the no-longer-member relay.
        rows = await self._remote_members.list_for_space(space_id)
        still_present = any(r.instance_id == instance_id for r in rows)
        if not still_present:
            await self._spaces.remove_space_instance(space_id, instance_id)
        await self._send_invite_envelope(
            to_instance_id=instance_id,
            event_type=FederationEventType.SPACE_REMOTE_MEMBER_REMOVED,
            payload={"space_id": space_id, "user_id": user_id},
        )
        await self._rotate_and_distribute_space_key(space_id)

    async def redeem_invite_token(
        self,
        token: str,
        *,
        user_id: str,
        issuer_instance_id: str | None = None,
    ) -> dict:
        """Consume an invite token, possibly via a cross-instance round-trip.

        When ``issuer_instance_id`` is None or matches our own instance,
        falls through to the local :meth:`accept_invite_token` and
        returns ``{space_id, role}``. Otherwise delegates to the
        attached :class:`SpaceInviteTokenRedeemCoordinator` which
        handshakes with the issuer and returns the same shape. Raises
        :class:`SpacePermissionError` (unpaired / banned / denied) or
        ``TimeoutError`` (issuer unreachable).
        """
        if issuer_instance_id is None or issuer_instance_id == self._own_instance_id:
            member = await self.accept_invite_token(token, user_id=user_id)
            return {"space_id": member.space_id, "role": member.role}
        if self._redeem_coordinator is None:
            raise SpacePermissionError(
                "cross-instance invite redeem is not available on this host",
            )
        return await self._redeem_coordinator.request_redeem(
            token,
            viewer_user_id=user_id,
            issuer_instance_id=issuer_instance_id,
        )

    async def accept_invite_token(
        self,
        token: str,
        *,
        user_id: str,
    ) -> SpaceMember:
        """Consume an invite token and enroll ``user_id`` as a member."""
        row = await self._spaces.consume_invite_token(token)
        if row is None:
            raise KeyError("invite token invalid, expired, or exhausted")
        space_id = row["space_id"]
        if await self._spaces.is_banned(space_id, user_id):
            raise SpacePermissionError(
                "banned from this space",
                banned=True,
            )
        member = SpaceMember(
            space_id=space_id,
            user_id=user_id,
            role=SpaceRole.MEMBER,
            joined_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._spaces.save_member(member)
        await self._bus.publish(
            SpaceMemberJoined(
                space_id=space_id,
                user_id=user_id,
                role=SpaceRole.MEMBER,
            )
        )
        return member

    async def request_join(
        self,
        space_id: str,
        *,
        user_id: str,
        message: str | None = None,
    ) -> str:
        space = await self._require_space(space_id)
        if space.join_mode is JoinMode.INVITE_ONLY:
            raise SpacePermissionError("space is invite-only")
        if await self._spaces.is_banned(space_id, user_id):
            raise SpacePermissionError("banned from this space", banned=True)
        existing = await self._spaces.get_member(space_id, user_id)
        if existing is not None:
            raise ValueError("already a member")
        request_id = await self._spaces.save_join_request(
            space_id,
            user_id,
            message=message,
        )
        await self._bus.publish(
            SpaceJoinRequested(
                space_id=space_id,
                user_id=user_id,
                request_id=request_id,
                message=message,
            )
        )
        return request_id

    async def approve_join_request(
        self,
        request_id: str,
        *,
        actor_username: str,
    ) -> SpaceMember | None:
        """Approve a pending join request.

        For local applicants, seats the user as a member and returns the
        :class:`SpaceMember`. For §D2 remote applicants, instead produces
        a short-TTL single-use invite token, fires it back via a
        :data:`SPACE_JOIN_REQUEST_APPROVED` envelope, and returns None —
        the applicant's household finalises the join with
        :meth:`accept_invite_token`.
        """
        actor = await self._users.get(actor_username)
        assert actor is not None
        space_id_row = await self._spaces._db.fetchone(  # type: ignore[attr-defined]
            """
            SELECT space_id, user_id,
                   remote_applicant_instance_id
              FROM space_join_requests WHERE id=?
            """,
            (request_id,),
        )
        row = row_to_dict(space_id_row)
        if row is None:
            raise KeyError(f"join request {request_id!r} not found")
        space = await self._require_space(row["space_id"])
        await self._require_admin_or_owner(space, actor_username)
        remote_instance = row.get("remote_applicant_instance_id")
        await self._spaces.update_join_request_status(
            request_id,
            "approved",
            reviewed_by=actor.user_id,
        )
        if remote_instance:
            # §D2 — cross-household approval. Mint an invite token and
            # federate it back; the applicant's household consumes via
            # the existing POST /api/spaces/join path.
            if self._federation is None:
                raise RuntimeError(
                    "space_service: federation not attached; "
                    "cannot approve remote join request",
                )
            expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            token = await self._spaces.create_invite_token(
                row["space_id"],
                created_by=actor.user_id,
                uses=1,
                expires_at=expires,
            )
            await self._federation.send_event(
                to_instance_id=remote_instance,
                event_type=FederationEventType.SPACE_JOIN_REQUEST_APPROVED,
                payload={
                    "request_id": request_id,
                    "space_id": row["space_id"],
                    "invite_token": token,
                    "reviewed_by": actor.user_id,
                },
            )
            await self._bus.publish(
                SpaceJoinApproved(
                    space_id=row["space_id"],
                    user_id=row["user_id"],
                    request_id=request_id,
                    approved_by=actor.user_id,
                )
            )
            return None

        member = SpaceMember(
            space_id=row["space_id"],
            user_id=row["user_id"],
            role=SpaceRole.MEMBER,
            joined_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._spaces.save_member(member)
        await self._bus.publish(
            SpaceJoinApproved(
                space_id=row["space_id"],
                user_id=row["user_id"],
                request_id=request_id,
                approved_by=actor.user_id,
            )
        )
        await self._bus.publish(
            SpaceMemberJoined(
                space_id=row["space_id"],
                user_id=row["user_id"],
                role=SpaceRole.MEMBER,
            )
        )
        return member

    async def deny_join_request(
        self,
        request_id: str,
        *,
        actor_username: str,
    ) -> None:
        actor = await self._users.get(actor_username)
        assert actor is not None
        # Look up the request first so we can emit the right event.
        row = await self._spaces._db.fetchone(  # type: ignore[attr-defined]
            """
            SELECT space_id, user_id, remote_applicant_instance_id
              FROM space_join_requests WHERE id=?
            """,
            (request_id,),
        )
        r = row_to_dict(row)
        await self._spaces.update_join_request_status(
            request_id,
            "denied",
            reviewed_by=actor.user_id,
        )
        if r is not None:
            remote_instance = r.get("remote_applicant_instance_id")
            if remote_instance and self._federation is not None:
                await self._federation.send_event(
                    to_instance_id=remote_instance,
                    event_type=FederationEventType.SPACE_JOIN_REQUEST_DENIED,
                    payload={
                        "request_id": request_id,
                        "space_id": r["space_id"],
                        "reviewed_by": actor.user_id,
                    },
                )
            await self._bus.publish(
                SpaceJoinDenied(
                    space_id=r["space_id"],
                    user_id=r["user_id"],
                    request_id=request_id,
                    denied_by=actor.user_id,
                )
            )

    async def request_join_remote(
        self,
        space_id: str,
        *,
        applicant_user_id: str,
        host_instance_id: str,
        message: str | None = None,
    ) -> str:
        """§D2 — applicant side: federate a join-request to a remote
        global-space host. The host must be a CONFIRMED peer. Persists
        a local pending-request row keyed by the generated
        ``request_id`` so :meth:`on_remote_join_request_approved` can
        match the inbound approval back to this user.
        """
        if self._federation is None or self._federation_repo is None:
            raise RuntimeError("federation not attached")
        peer = await self._federation_repo.get_instance(host_instance_id)
        if peer is None or peer.status is not PairingStatus.CONFIRMED:
            raise SpacePermissionError(
                "host household is not a CONFIRMED peer — pair first",
            )
        # Persist locally so the inbound APPROVED handler can look up
        # the applicant_user_id; there's no host-side space row locally.
        # §Audit #11: route through ``save_join_request`` rather than
        # poking the repo's private ``_db`` — no SQL in services.
        request_id = await self._spaces.save_join_request(
            space_id,
            applicant_user_id,
            message=message,
            ttl_days=7,
            remote_applicant_instance_id=host_instance_id,
        )
        await self._federation.send_event(
            to_instance_id=host_instance_id,
            event_type=FederationEventType.SPACE_JOIN_REQUEST,
            payload={
                "request_id": request_id,
                "space_id": space_id,
                "user_id": applicant_user_id,
                "message": message,
            },
            space_id=space_id,
        )
        return request_id

    async def on_remote_join_request_approved(
        self,
        request_id: str,
        *,
        invite_token: str,
    ) -> None:
        """Auto-consume the invite token returned with a
        :data:`SPACE_JOIN_REQUEST_APPROVED` envelope so the applicant
        becomes a space member without further UI clicks.
        """
        row = await self._spaces._db.fetchone(  # type: ignore[attr-defined]
            "SELECT user_id FROM space_join_requests WHERE id=?",
            (request_id,),
        )
        r = row_to_dict(row)
        if r is None:
            return
        user_id = r.get("user_id")
        if not user_id:
            return
        try:
            await self.accept_invite_token(invite_token, user_id=user_id)
        except KeyError, SpacePermissionError:
            # Token already consumed or user now banned.
            pass

    # ── Space posts ────────────────────────────────────────────────────

    async def create_post(
        self,
        space_id: str,
        *,
        author_user_id: str,
        type: PostType | str,
        content: str | None = None,
        media_url: str | None = None,
        image_urls: tuple[str, ...] | list[str] = (),
        file_meta: FileMeta | None = None,
        location: LocationData | None = None,
        linked_highlight_id: str | None = None,
    ) -> Post | None:
        """Create a post in the space, subject to the feature's access level.

        Returns the persisted :class:`Post` for `open` / admin paths. For
        `moderated` access where the author isn't an admin, the content
        enters the moderation queue and this method returns ``None`` after
        publishing :class:`SpaceModerationQueued`.
        """
        space = await self._require_space(space_id)
        author = await self._users.get_by_user_id(author_user_id)
        if author is None:
            raise KeyError(f"user {author_user_id!r} not found")
        member = await self._spaces.get_member(space_id, author_user_id)
        if member is None:
            raise SpacePermissionError("not a member of this space")
        self._assert_writable_member(member, action="post")
        if not space.features.allows(type):
            raise SpacePermissionError(f"space does not allow {type!r} posts")

        post_type = _coerce_post_type(type)
        image_urls_tuple = tuple(image_urls)
        _validate_space_content(
            post_type,
            content,
            file_meta,
            location,
            image_urls_tuple,
        )
        is_admin = member.role in (SpaceRole.OWNER, SpaceRole.ADMIN)
        decision = space.features.access_decision("posts", is_admin=is_admin)
        if decision == "deny":
            raise SpacePermissionError("posting is admin-only in this space")

        # Truncate to 4dp at the service boundary regardless of what the
        # client sent — the column never holds higher precision than the
        # federated form (§GPS truncation).
        if location is not None:
            location = LocationData(
                lat=truncate_coord(location.lat) or 0.0,
                lon=truncate_coord(location.lon) or 0.0,
                label=location.label,
            )

        post = Post(
            id=uuid.uuid4().hex,
            author=author.user_id,
            type=post_type,
            created_at=datetime.now(timezone.utc),
            content=content,
            media_url=None if post_type is PostType.IMAGE else media_url,
            image_urls=image_urls_tuple,
            file_meta=file_meta,
            location=location,
            linked_highlight_id=linked_highlight_id,
        )
        if decision == "queue":
            now = datetime.now(timezone.utc)
            item = SpaceModerationItem(
                id=uuid.uuid4().hex,
                space_id=space_id,
                feature="posts",
                action="create",
                submitted_by=author.user_id,
                payload={
                    "post_id": post.id,
                    "type": post_type.value,
                    "content": content,
                    "media_url": media_url,
                    "file_meta": _file_meta_to_payload(file_meta),
                    "location": (
                        {
                            "lat": location.lat,
                            "lon": location.lon,
                            "label": location.label,
                        }
                        if location is not None
                        else None
                    ),
                },
                current_snapshot=None,
                submitted_at=now,
                expires_at=now + timedelta(days=7),
                status=ModerationStatus.PENDING,
            )
            await self._spaces.save_moderation_item(item)
            await self._bus.publish(SpaceModerationQueued(item=item))
            return None

        await self._persist_post(space_id, post)
        return post

    async def _persist_post(self, space_id: str, post: Post) -> Post:
        """Persist a Post and publish SpacePostCreated.

        Shared by the direct ``create_post`` path and the moderation-approve
        path so both produce identical state transitions and federation
        broadcasts.
        """
        await self._posts.save(space_id, post)
        await self._bus.publish(
            SpacePostCreated(
                post=post,
                space_id=space_id,
            )
        )
        return post

    # ── Moderation queue admin API ─────────────────────────────────────

    async def list_pending_moderation(
        self,
        space_id: str,
        *,
        actor_username: str,
    ) -> list[SpaceModerationItem]:
        """List pending queue items (admin-only)."""
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        return await self._spaces.list_moderation_queue(
            space_id,
            status=ModerationStatus.PENDING,
        )

    async def approve_moderation_item(
        self,
        space_id: str,
        item_id: str,
        *,
        actor_username: str,
    ) -> Post:
        """Approve a queued post. Persists the post and marks the item
        APPROVED. Raises :class:`ModerationAlreadyDecidedError` if the
        item is not in ``PENDING`` status.
        """
        space = await self._require_space(space_id)
        actor = await self._require_admin_or_owner(space, actor_username)
        item = await self._spaces.get_moderation_item(item_id)
        if item is None or item.space_id != space_id:
            raise KeyError(f"moderation item {item_id!r} not found")
        if item.status is not ModerationStatus.PENDING:
            raise ModerationAlreadyDecidedError(
                f"item {item_id!r} is already {item.status.value}",
            )

        post = _post_from_queue_payload(item)
        await self._persist_post(space_id, post)
        await self._spaces.update_moderation_item_status(
            item_id,
            status=ModerationStatus.APPROVED,
            reviewed_by=actor.user_id,
        )
        approved = replace(
            item,
            status=ModerationStatus.APPROVED,
            reviewed_by=actor.user_id,
            reviewed_at=datetime.now(timezone.utc),
        )
        await self._bus.publish(SpaceModerationApproved(item=approved))
        return post

    async def reject_moderation_item(
        self,
        space_id: str,
        item_id: str,
        *,
        actor_username: str,
        reason: str | None = None,
    ) -> None:
        """Reject a queued item; item status becomes REJECTED."""
        space = await self._require_space(space_id)
        actor = await self._require_admin_or_owner(space, actor_username)
        item = await self._spaces.get_moderation_item(item_id)
        if item is None or item.space_id != space_id:
            raise KeyError(f"moderation item {item_id!r} not found")
        if item.status is not ModerationStatus.PENDING:
            raise ModerationAlreadyDecidedError(
                f"item {item_id!r} is already {item.status.value}",
            )

        await self._spaces.update_moderation_item_status(
            item_id,
            status=ModerationStatus.REJECTED,
            reviewed_by=actor.user_id,
            rejection_reason=reason,
        )
        rejected = replace(
            item,
            status=ModerationStatus.REJECTED,
            reviewed_by=actor.user_id,
            reviewed_at=datetime.now(timezone.utc),
            rejection_reason=reason,
        )
        await self._bus.publish(SpaceModerationRejected(item=rejected))

    async def edit_post(
        self,
        post_id: str,
        *,
        editor_user_id: str,
        new_content: str,
    ) -> Post:
        got = await self._posts.get(post_id)
        if got is None:
            raise KeyError(f"space post {post_id!r} not found")
        space_id, post = got
        if post.deleted:
            raise KeyError("post already deleted")
        # Verifies space exists — raises KeyError if not.
        await self._require_space(space_id)
        if post.author != editor_user_id:
            # Admin override
            editor = await self._users.get_by_user_id(editor_user_id)
            if editor is None:
                raise PermissionError("not authorised")
            member = await self._spaces.get_member(space_id, editor_user_id)
            if member is None or member.role not in (SpaceRole.OWNER, SpaceRole.ADMIN):
                raise PermissionError("only the author or a space admin can edit")
        _validate_text_length(new_content, limit=MAX_POST_LENGTH)
        await self._posts.edit(post_id, new_content)
        refreshed = await self._posts.get(post_id)
        assert refreshed is not None  # just edited — must exist
        # Bus fan-out so subscribers (system-album bridge, search index,
        # SPACE_POST_UPDATED federation outbound) can react. ``space_id``
        # gates the federation broadcast so we don't accidentally fan
        # a household-feed edit out to space members.
        await self._bus.publish(PostEdited(post=refreshed[1], space_id=space_id))
        return refreshed[1]

    async def delete_post(
        self,
        post_id: str,
        *,
        actor_user_id: str,
    ) -> None:
        got = await self._posts.get(post_id)
        if got is None:
            raise KeyError(f"space post {post_id!r} not found")
        space_id, post = got
        if post.deleted:
            return
        moderated_by: str | None = None
        if post.author != actor_user_id:
            # Moderation path — actor must be admin/owner
            member = await self._spaces.get_member(space_id, actor_user_id)
            if member is None or member.role not in (SpaceRole.OWNER, SpaceRole.ADMIN):
                raise PermissionError("only the author or a space admin can delete")
            moderated_by = actor_user_id
        await self._posts.soft_delete(post_id, moderated_by=moderated_by)
        if moderated_by is not None:
            refreshed = await self._posts.get(post_id)
            assert refreshed is not None  # just soft-deleted — row still exists
            await self._bus.publish(
                SpacePostModerated(
                    space_id=space_id,
                    post=refreshed[1],
                    moderated_by=actor_user_id,
                )
            )
        # Generic post-deleted event — fires on both author + moderation
        # paths so cross-cutting subscribers (system-album bridge, search
        # index, federation outbound for SPACE_POST_DELETED) have a single
        # hook regardless of who deleted the row. ``space_id`` gates the
        # outbound broadcast so household-feed deletes stay local.
        await self._bus.publish(PostDeleted(post_id=post_id, space_id=space_id))

    async def add_reaction(
        self,
        post_id: str,
        *,
        user_id: str,
        emoji: str,
    ) -> Post:
        emoji = unicodedata.normalize("NFC", emoji.strip())
        if not emoji:
            raise ValueError("emoji must not be empty")
        got = await self._posts.get(post_id)
        if got is not None:
            space_id, _post = got
            await self._reject_subscriber(space_id, user_id, action="react")
        return await self._posts.add_reaction(post_id, emoji, user_id)

    async def remove_reaction(
        self,
        post_id: str,
        *,
        user_id: str,
        emoji: str,
    ) -> Post:
        emoji = unicodedata.normalize("NFC", emoji.strip())
        got = await self._posts.get(post_id)
        if got is not None:
            space_id, _post = got
            await self._reject_subscriber(space_id, user_id, action="react")
        return await self._posts.remove_reaction(post_id, emoji, user_id)

    async def add_comment(
        self,
        post_id: str,
        *,
        author_user_id: str,
        content: str | None = None,
        media_url: str | None = None,
        parent_id: str | None = None,
        comment_type: CommentType | str = CommentType.TEXT,
    ) -> Comment:
        got = await self._posts.get(post_id)
        if got is None:
            raise KeyError(f"space post {post_id!r} not found")
        space_id, post = got
        if post.deleted:
            raise KeyError("cannot comment on deleted post")
        # Membership check
        member = await self._spaces.get_member(space_id, author_user_id)
        if member is None:
            raise SpacePermissionError("not a member of this space")
        # Subscribers may comment when the space's
        # ``allow_subscriber_comment`` opt-in is on.  Non-subscribers
        # short-circuit before the (cheap) space lookup.
        space = (
            await self._spaces.get(space_id)
            if member.role == SpaceRole.SUBSCRIBER
            else None
        )
        self._assert_writable_member(member, action="comment", space=space)
        ctype = _coerce_comment_type(comment_type)
        if ctype is CommentType.TEXT:
            _validate_text_length(content, limit=MAX_COMMENT_LENGTH)
            if not content or not content.strip():
                raise ValueError("comment content required")
        elif ctype is CommentType.IMAGE and not media_url:
            raise ValueError("image comment requires media_url")
        if parent_id is not None:
            parent = await self._posts.get_comment(parent_id)
            if parent is None or parent.post_id != post_id:
                raise KeyError(f"parent comment {parent_id!r} not in this post")
        comment = Comment(
            id=uuid.uuid4().hex,
            post_id=post.id,
            author=author_user_id,
            type=ctype,
            created_at=datetime.now(timezone.utc),
            parent_id=parent_id,
            content=content,
            media_url=media_url,
        )
        await self._posts.add_comment(comment)
        await self._posts.increment_comment_count(post_id)
        await self._bus.publish(
            CommentAdded(post_id=post_id, comment=comment, space_id=space_id),
        )
        return comment

    async def edit_comment(
        self,
        comment_id: str,
        *,
        editor_user_id: str,
        new_content: str,
    ) -> Comment:
        """Edit a space comment's body. Author-or-space-admin only."""
        comment = await self._posts.get_comment(comment_id)
        if comment is None or comment.deleted:
            raise KeyError(f"comment {comment_id!r} not found")
        if comment.type is not CommentType.TEXT:
            raise ValueError("only text comments can be edited")
        got = await self._posts.get(comment.post_id)
        if got is None:
            raise KeyError("post disappeared")
        space_id, _post = got
        if comment.author != editor_user_id:
            member = await self._spaces.get_member(space_id, editor_user_id)
            if member is None or member.role not in (SpaceRole.OWNER, SpaceRole.ADMIN):
                raise PermissionError(
                    "only the author or a space admin can edit this comment",
                )
        _validate_text_length(new_content, limit=MAX_COMMENT_LENGTH)
        if not new_content.strip():
            raise ValueError("comment body cannot be empty")
        await self._posts.edit_comment(comment_id, new_content)
        updated = await self._posts.get_comment(comment_id)
        assert updated is not None
        await self._bus.publish(
            CommentUpdated(
                post_id=updated.post_id,
                comment=updated,
                space_id=space_id,
            ),
        )
        return updated

    async def delete_comment(
        self,
        comment_id: str,
        *,
        actor_user_id: str,
    ) -> None:
        comment = await self._posts.get_comment(comment_id)
        if comment is None:
            raise KeyError(f"comment {comment_id!r} not found")
        if comment.deleted:
            return
        got = await self._posts.get(comment.post_id)
        if got is None:
            raise KeyError("post disappeared")
        space_id, _post = got
        if comment.author != actor_user_id:
            member = await self._spaces.get_member(space_id, actor_user_id)
            if member is None or member.role not in (SpaceRole.OWNER, SpaceRole.ADMIN):
                raise PermissionError(
                    "only the author or a space admin can delete this comment"
                )
        await self._posts.soft_delete_comment(comment_id)
        await self._posts.decrement_comment_count(comment.post_id)
        await self._bus.publish(
            CommentDeleted(
                post_id=comment.post_id,
                comment_id=comment_id,
                space_id=space_id,
            ),
        )

    async def list_feed(
        self,
        space_id: str,
        *,
        before: str | None = None,
        limit: int = 20,
    ) -> list[Post]:
        await self._require_space(space_id)
        limit = max(1, min(int(limit), 50))
        return await self._posts.list_feed(space_id, before=before, limit=limit)

    # ── Sidebar pins + aliases (convenience) ───────────────────────────

    async def pin(
        self,
        user_id: str,
        space_id: str,
        position: int = 0,
    ) -> None:
        await self._require_space(space_id)
        await self._spaces.pin_sidebar(user_id, space_id, int(position))

    async def unpin(self, user_id: str, space_id: str) -> None:
        await self._spaces.unpin_sidebar(user_id, space_id)

    async def set_alias(
        self,
        space_id: str,
        *,
        username: str,
        alias: str,
    ) -> None:
        await self._require_space(space_id)
        await self._spaces.set_space_alias(space_id, username, alias)

    # ── Subscriptions (read-only membership) ───────────────────────────
    #
    # Subscribe = add self to the space as a :class:`SpaceMember` with
    # ``role='subscriber'``. Same content delivery as real members: the
    # normal sync + fan-out stream applies. Write paths (post / comment
    # / reaction) gate on role and reject subscribers — they're strictly
    # read-only.
    #
    # Note the name: we use *subscribe* rather than *follow* because the
    # frontend already uses "followed spaces" for a different concept
    # (a dashboard pin list over spaces the user is a full member of —
    # see ``users.preferences_json['followed_space_ids']`` and
    # ``corner_service``). The two features are distinct on purpose.
    #
    # Constraints:
    # * Only ``PUBLIC`` and ``GLOBAL`` spaces are subscribable. Private /
    #   household spaces require an invite.
    # * If the caller is already a member (any non-subscriber role),
    #   subscribe is a no-op — we do not demote real members.
    # * Subscribe respects bans + the §CP.F1 age gate, same as
    #   ``add_member``.

    async def subscribe_to_space(self, user_id: str, space_id: str) -> None:
        space = await self._require_space(space_id)
        if space.space_type not in (SpaceType.PUBLIC, SpaceType.GLOBAL):
            raise SpacePermissionError(
                "only public / global spaces can be subscribed to",
            )
        if await self._spaces.is_banned(space_id, user_id):
            raise SpacePermissionError(
                f"user {user_id!r} is banned from this space",
                banned=True,
            )
        existing = await self._spaces.get_member(space_id, user_id)
        if existing is not None:
            # Already a member (any role) — no-op. Never demote.
            return
        if self._child_protection is not None:
            await self._child_protection.check_space_age_gate(space_id, user_id)
        member = SpaceMember(
            space_id=space_id,
            user_id=user_id,
            role=SpaceRole.SUBSCRIBER,
            joined_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._spaces.save_member(member)
        await self._bus.publish(
            SpaceMemberJoined(
                space_id=space_id,
                user_id=user_id,
                role=SpaceRole.SUBSCRIBER,
            )
        )
        if self._child_protection is not None:
            await self._child_protection.record_membership_change(
                user_id=user_id,
                space_id=space_id,
                action="joined",
                actor_id=user_id,
            )

    async def unsubscribe_from_space(self, user_id: str, space_id: str) -> None:
        """Remove a self-subscription. No-op if the user isn't a
        subscriber — real members must use ``remove_member`` /
        ``leave_space`` (we refuse to silently demote them by
        "unsubscribing")."""
        existing = await self._spaces.get_member(space_id, user_id)
        if existing is None or existing.role != SpaceRole.SUBSCRIBER:
            return
        await self._spaces.delete_member(space_id, user_id)
        await self._bus.publish(
            SpaceMemberLeft(
                space_id=space_id,
                user_id=user_id,
            )
        )
        if self._child_protection is not None:
            await self._child_protection.record_membership_change(
                user_id=user_id,
                space_id=space_id,
                action="removed",
                actor_id=user_id,
            )

    async def list_subscriptions(self, user_id: str) -> list[dict]:
        return await self._spaces.list_subscriptions_for_user(user_id)

    async def is_subscribed(self, user_id: str, space_id: str) -> bool:
        member = await self._spaces.get_member(space_id, user_id)
        return member is not None and member.role == SpaceRole.SUBSCRIBER

    # ── Sidebar links (§23 — admin-configurable quick-links) ───────────

    async def list_links(self, space_id: str, *, actor_user_id: str) -> list[dict]:
        await self._require_member(space_id, actor_user_id)
        return await self._spaces.list_links(space_id)

    async def upsert_link(
        self,
        *,
        space_id: str,
        actor_username: str,
        link_id: str | None,
        label: str,
        url: str,
        position: int,
    ) -> dict:
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        label = label.strip()
        url = url.strip()
        if not label:
            raise ValueError("label must not be empty")
        if not url:
            raise ValueError("url must not be empty")
        link_id = link_id or uuid.uuid4().hex
        await self._spaces.upsert_link(
            link_id=link_id,
            space_id=space_id,
            label=label,
            url=url,
            position=int(position),
        )
        return {
            "id": link_id,
            "label": label,
            "url": url,
            "position": int(position),
        }

    async def delete_link(
        self,
        *,
        link_id: str,
        actor_username: str,
    ) -> None:
        link = await self._spaces.get_link(link_id)
        if link is None:
            raise KeyError(f"link {link_id!r} not found")
        space = await self._require_space(link["space_id"])
        await self._require_admin_or_owner(space, actor_username)
        await self._spaces.delete_link(link_id)

    # ── Internal helpers ───────────────────────────────────────────────

    async def _require_space(self, space_id: str) -> Space:
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved:
            raise KeyError(f"space {space_id!r} not found")
        return space

    async def _require_member(
        self,
        space_id: str,
        user_id: str,
    ) -> SpaceMember:
        member = await self._spaces.get_member(space_id, user_id)
        if member is None:
            raise SpacePermissionError("not a member of this space")
        return member

    async def _reject_subscriber(
        self,
        space_id: str,
        user_id: str,
        *,
        action: str = "post",
        space: "Space | None" = None,
    ) -> None:
        """Raise :class:`SpacePermissionError` if ``user_id`` is a
        subscriber of ``space_id`` and the action isn't admin-opted-in
        for subscribers.  Reactions and comments can be opted in via
        ``SpaceFeatures.allow_subscriber_react`` /
        ``allow_subscriber_comment``; posts always remain member-only.

        Used on write paths that haven't already fetched the member
        row.  For paths that have already fetched the member row,
        prefer :meth:`_assert_writable_member` to avoid a second
        lookup.
        """
        member = await self._spaces.get_member(space_id, user_id)
        if member is None:
            return
        if space is None and member.role == SpaceRole.SUBSCRIBER:
            space = await self._spaces.get(space_id)
        self._assert_writable_member(member, action=action, space=space)

    @staticmethod
    def _assert_writable_member(
        member: SpaceMember,
        *,
        action: str = "post",
        space: "Space | None" = None,
    ) -> None:
        """Raise :class:`SpacePermissionError` if ``member`` is a
        read-only subscriber and the action isn't admin-opted-in for
        subscribers.  Use on every write path after the membership
        lookup.
        """
        if member.role != SpaceRole.SUBSCRIBER:
            return
        # Subscriber-engagement opt-ins (§23.49).  Admins may allow
        # subscribers to react and/or comment without making them full
        # members.  Posts (action=='post') stay strictly member-only.
        if space is not None:
            if action == "react" and space.features.allow_subscriber_react:
                return
            if action == "comment" and space.features.allow_subscriber_comment:
                return
        raise SpacePermissionError(
            f"subscribers can only read — joining as a member is required to {action}",
        )

    async def _require_admin_or_owner(
        self,
        space: Space,
        actor_username: str,
    ) -> SpaceMember:
        actor = await self._users.get(actor_username)
        if actor is None:
            raise KeyError(f"actor {actor_username!r} not found")
        member = await self._spaces.get_member(space.id, actor.user_id)
        if member is None or member.role not in (SpaceRole.OWNER, SpaceRole.ADMIN):
            raise SpacePermissionError("admin or owner required")
        return member

    async def _require_owner(
        self,
        space: Space,
        actor_username: str,
    ) -> SpaceMember:
        actor = await self._users.get(actor_username)
        if actor is None:
            raise KeyError(f"actor {actor_username!r} not found")
        member = await self._spaces.get_member(space.id, actor.user_id)
        if member is None or member.role != SpaceRole.OWNER:
            raise SpacePermissionError("owner required")
        return member


# ─── Helpers ──────────────────────────────────────────────────────────────


async def build_space_snapshot_for_federation(
    space: Space,
    *,
    space_repo,
    remote_member_repo,
    user_repo,
    own_instance_id: str,
    cover_repo=None,
    space_crypto_service=None,
) -> dict:
    """:func:`_space_metadata_for_federation` + a roster of every
    member of this space.

    The roster lets a §D1b joiner mirror the full member list
    locally — without it the joiner's Members tab on a stub of
    Pascal's space shows only herself. Each row carries
    ``(user_id, instance_id, display_name, role, joined_at)`` —
    the minimum the receiver needs to write a
    :class:`SpaceRemoteMember` row that the route handler then
    merges into ``GET /api/spaces/{id}/members``.

    The host's own local users ship with ``instance_id`` set to
    *our* instance, because from the joiner's perspective every
    member of this space lives somewhere else.
    """
    meta = _space_metadata_for_federation(space)
    local_members = await space_repo.list_members(space.id)
    local_users = await user_repo.list_by_ids({m.user_id for m in local_members})
    name_by_id = {u.user_id: u.display_name for u in local_users}
    roster: list[dict] = []
    for m in local_members:
        roster.append(
            {
                "user_id": m.user_id,
                "instance_id": own_instance_id,
                "display_name": name_by_id.get(m.user_id, ""),
                "role": m.role,
                "joined_at": m.joined_at,
            }
        )
    if remote_member_repo is not None:
        for r in await remote_member_repo.list_for_space(space.id):
            roster.append(
                {
                    "user_id": r.user_id,
                    "instance_id": r.instance_id,
                    "display_name": r.display_name or "",
                    "role": "member",
                    "joined_at": r.joined_at or "",
                    # Federated peers carry a public_key on the host
                    # side; ship it so the joiner can record it too
                    # and later verify signed events from this user.
                    "user_pk": r.user_pk,
                }
            )
    meta["roster"] = roster
    # §D1b cover federation (#116) — ship the actual WebP bytes
    # alongside ``cover_hash``. Without them, the joiner's stub
    # renders the gradient fallback even when the host has a
    # custom cover. Capped at SPACE_COVER_MAX_DIMENSION on the
    # host side, so the payload stays under ~150 kB even at the
    # densest end. Base64 because the envelope is JSON.
    if cover_repo is not None and space.cover_hash:
        cover = await cover_repo.get(space.id)
        if cover is not None:
            bytes_webp, _hash = cover
            meta["cover_webp_base64"] = base64.b64encode(bytes_webp).decode("ascii")
    # §D1b content-key handoff (#117) — the space content key is the
    # symmetric AES-256 secret that decrypts every event in this
    # space. Ship it inside the (already-encrypted to the invitee
    # instance) envelope so the new member can read posts, comments,
    # reactions etc. without us having to bolt on per-user
    # asymmetric key delivery. The envelope-level encryption is the
    # security contract here: §D1b promises GFS sees only routing,
    # and an attacker who can already read the envelope payload is
    # the host on either end and already has the key. NEVER ship
    # this dict outside an encrypted federation envelope.
    if space_crypto_service is not None:
        key_info = await space_crypto_service.export_current_key(space.id)
        if key_info is not None:
            epoch, raw_key = key_info
            # ``key_suite`` is the forward-compat lever — see
            # :data:`KEY_SUITE_AESGCM_256` in space_crypto_service.
            # Mirrors the ``kem_suite`` convention in
            # ``routed_crypto.py`` so a future PQ-protected variant
            # is a wire-additive change; older receivers reject
            # unknown suites rather than silently fall back.
            meta["space_content_key"] = {
                "epoch": epoch,
                "key_suite": KEY_SUITE_AESGCM_256,
                "key_base64": base64.b64encode(raw_key).decode("ascii"),
            }
    return meta


def _space_metadata_for_federation(space: Space) -> dict:
    """Snapshot the space's user-visible config for federation envelopes.

    Used by the §D1b invite + redeem-ACK paths so the joiner's
    instance has enough material to seat a local stub row in
    ``spaces`` (name, owner identity, feature toggles, etc.). Mirrors
    the columns ``Space`` already exposes — we deliberately don't
    include host-private state (admins list, ban list, cover bytes;
    those federate over their own dedicated events).
    """
    return {
        "name": space.name,
        "emoji": space.emoji,
        "description": space.description,
        "owner_instance_id": space.owner_instance_id,
        "owner_username": space.owner_username,
        "identity_public_key": space.identity_public_key,
        "config_sequence": space.config_sequence,
        "space_type": space.space_type.value,
        "join_mode": space.join_mode.value,
        "features": {
            "pages": space.features.pages,
            "calendar": space.features.calendar,
            "todo": space.features.todo,
            "location": space.features.location,
            "location_mode": space.features.location_mode,
            "stickies": space.features.stickies,
            "gallery": space.features.gallery,
        },
        "tz": space.tz,
        "cover_hash": space.cover_hash,
        "about_markdown": space.about_markdown,
    }


async def apply_space_content_key_from_metadata(
    space_id: str,
    *,
    meta: dict,
    space_crypto_service,
) -> None:
    """Persist the §D1b shipped space content key on the receiver side.

    The envelope carrying ``meta`` is itself encrypted to this
    instance (§D1b zero-leak), so by the time we read
    ``meta["space_content_key"]`` it's plaintext only inside this
    process. We immediately re-wrap with the local KEK via
    :meth:`SpaceContentEncryption.import_key` so it lands in
    ``space_keys`` with the same at-rest shape every locally-minted
    key has — the at-rest invariant is "wrapped by THIS instance's
    KEK", and the import path preserves it.

    No-op when the receiver doesn't have a SpaceContentEncryption
    service wired (e.g. test stacks with no KEK), when the host
    didn't ship a key (legacy sender, or pre-key-init space), or
    when the payload is malformed (defensive — we'd rather keep
    the user able to *see* the stub than crash the accept handler).
    """
    if space_crypto_service is None:
        return
    payload = meta.get("space_content_key")
    if not isinstance(payload, dict):
        return
    # Forward-compat lever — receivers reject unknown suites rather
    # than fall back to a default. Senders that don't include
    # ``key_suite`` (this build's first revision) default to the
    # single value we support today.
    suite = payload.get("key_suite", KEY_SUITE_AESGCM_256)
    if suite not in SUPPORTED_KEY_SUITES:
        log.warning(
            "apply_space_content_key_from_metadata: unsupported key_suite "
            "%r for %s; receiver will stay unable to decrypt until "
            "upgraded.",
            suite,
            space_id,
        )
        raise UnsupportedKeySuite(
            f"space content key advertises unsupported key_suite={suite!r}; "
            f"this build supports {sorted(SUPPORTED_KEY_SUITES)!r}",
        )
    epoch = payload.get("epoch")
    key_b64 = payload.get("key_base64")
    if not isinstance(key_b64, str) or epoch is None:
        return
    try:
        raw = base64.b64decode(key_b64)
    except Exception:  # pragma: no cover — defensive
        log.warning(
            "apply_space_content_key_from_metadata: invalid base64 for %s",
            space_id,
        )
        return
    if len(raw) != 32:
        log.warning(
            "apply_space_content_key_from_metadata: wrong key length %d for %s",
            len(raw),
            space_id,
        )
        return
    try:
        await space_crypto_service.import_key(space_id, int(epoch), raw)
    except Exception:  # pragma: no cover — defensive
        log.exception(
            "apply_space_content_key_from_metadata: import_key raised for %s",
            space_id,
        )


async def apply_space_cover_from_metadata(
    space_id: str,
    *,
    meta: dict,
    cover_repo,
) -> None:
    """When a §D1b stub-creation event carries the host's WebP cover
    bytes (``meta['cover_webp_base64']``), decode + persist them via
    the supplied cover repo so the joiner's ``/api/spaces/{id}/cover``
    serves the real image instead of the gradient placeholder.

    No-op when ``cover_repo`` isn't wired, when the host didn't ship
    bytes (older sender), or when base64 decoding fails (the receiver
    just keeps the gradient fallback rather than crashing).
    """
    if cover_repo is None:
        return
    b64 = meta.get("cover_webp_base64")
    if not isinstance(b64, str) or not b64:
        return
    try:
        bytes_webp = base64.b64decode(b64)
    except Exception:  # pragma: no cover — defensive
        log.warning(
            "apply_space_cover_from_metadata: invalid base64 for space %s",
            space_id,
        )
        return
    cover_hash = str(meta.get("cover_hash") or "")
    if not cover_hash:
        return
    # ``width`` / ``height`` aren't shipped today — the SPA renders
    # the cover at whatever native dimensions the WebP carries, so
    # passing 0 here is fine. The repo signature requires the
    # kwargs; later we can ship dimensions in ``space_meta`` too.
    await cover_repo.set(
        space_id,
        bytes_webp=bytes_webp,
        hash=cover_hash,
        width=0,
        height=0,
    )


def stub_space_from_metadata(
    space_id: str,
    *,
    host_instance_id: str,
    meta: dict,
) -> Space:
    """Build a :class:`Space` from a federation metadata payload (the
    counterpart to :func:`_space_metadata_for_federation`).

    Used by the §D1b inbound paths — ``PrivateSpaceInviteHandler``
    on receipt of ``SPACE_PRIVATE_INVITE`` and the receiver side of
    ``SpaceInviteTokenRedeemCoordinator`` on receipt of the ACK —
    to seat a local **stub** row in ``spaces``. The joiner doesn't
    own the space, but having the row locally is what makes the
    space show up in their ``/api/spaces`` once they accept and a
    matching ``space_members`` row is inserted.

    ``host_instance_id`` is the envelope's ``from_instance`` and
    falls back into ``owner_instance_id`` if the payload doesn't
    carry it (older senders).  Either way, the resulting Space's
    ``owner_instance_id != my_instance`` is the runtime signal for
    "this is a remote space" downstream.
    """
    feats_in = meta.get("features") or {}
    raw_mode = feats_in.get("location_mode")
    location_mode: "Literal['gps', 'zone_only']" = (
        "zone_only" if raw_mode == "zone_only" else "gps"
    )
    features = SpaceFeatures(
        calendar=bool(feats_in.get("calendar", True)),
        todo=bool(feats_in.get("todo", True)),
        location=bool(feats_in.get("location", False)),
        location_mode=location_mode,
        stickies=bool(feats_in.get("stickies", True)),
        pages=bool(feats_in.get("pages", True)),
        gallery=bool(feats_in.get("gallery", True)),
    )
    return Space(
        id=space_id,
        name=str(meta.get("name") or "Untitled space"),
        emoji=meta.get("emoji"),
        description=meta.get("description"),
        owner_instance_id=str(meta.get("owner_instance_id") or host_instance_id),
        owner_username=str(meta.get("owner_username") or ""),
        identity_public_key=str(meta.get("identity_public_key") or ""),
        config_sequence=int(meta.get("config_sequence") or 0),
        features=features,
        space_type=_coerce_space_type(meta.get("space_type") or "private"),
        join_mode=_coerce_join_mode(meta.get("join_mode") or "invite_only"),
        tz=str(meta.get("tz") or "UTC"),
        cover_hash=meta.get("cover_hash"),
        about_markdown=meta.get("about_markdown"),
    )


def _coerce_space_type(value: SpaceType | str) -> SpaceType:
    if isinstance(value, SpaceType):
        return value
    try:
        return SpaceType(value)
    except ValueError as exc:
        raise ValueError(f"invalid space type {value!r}") from exc


def _coerce_join_mode(value: JoinMode | str) -> JoinMode:
    if isinstance(value, JoinMode):
        return value
    try:
        return JoinMode(value)
    except ValueError as exc:
        raise ValueError(f"invalid join mode {value!r}") from exc


def _coerce_post_type(value: PostType | str) -> PostType:
    if isinstance(value, PostType):
        return value
    try:
        return PostType(value)
    except ValueError as exc:
        raise ValueError(f"invalid post type {value!r}") from exc


def _coerce_comment_type(value: CommentType | str) -> CommentType:
    if isinstance(value, CommentType):
        return value
    try:
        return CommentType(value)
    except ValueError as exc:
        raise ValueError(f"invalid comment type {value!r}") from exc


#: Cap for the optional location-post label. Mirrors
#: feed_service.LOCATION_LABEL_MAX so the household + space surfaces
#: agree.
LOCATION_LABEL_MAX = 80


def _validate_space_content(
    post_type: PostType,
    content: str | None,
    file_meta: FileMeta | None,
    location: LocationData | None = None,
    image_urls: tuple[str, ...] = (),
) -> None:
    if post_type is PostType.FILE and file_meta is None:
        raise ValueError("file post requires file_meta")
    if post_type is PostType.IMAGE:
        if not image_urls:
            raise ValueError("image post requires at least one image_url")
        if len(image_urls) > FEED_POST_MAX_IMAGES:
            raise ValueError(
                f"image post may carry at most {FEED_POST_MAX_IMAGES} images",
            )
    if post_type is PostType.LOCATION:
        if location is None:
            raise ValueError("location post requires lat/lon")
        if location.label is not None and len(location.label) > LOCATION_LABEL_MAX:
            raise ValueError(
                f"location label exceeds {LOCATION_LABEL_MAX} characters",
            )
    if post_type in (PostType.TEXT, PostType.TRANSCRIPT):
        if not content or not content.strip():
            raise ValueError(f"{post_type.value} post requires content")
    if post_type is not PostType.IMAGE and image_urls:
        raise ValueError(
            f"{post_type.value} post must not carry image_urls",
        )
    _validate_text_length(content, limit=MAX_POST_LENGTH)


def _validate_text_length(
    content: str | None,
    *,
    limit: int,
) -> None:
    if content is None:
        return
    if len(content) > limit:
        raise ValueError(f"content exceeds maximum length of {limit} characters")


def _round4(value: float | None) -> float | None:
    """Truncate a GPS coordinate to 4dp (§25 rule)."""
    if value is None:
        return None
    return round(float(value), 4)


def _file_meta_to_payload(fm: FileMeta | None) -> dict | None:
    if fm is None:
        return None
    return {
        "url": fm.url,
        "mime_type": fm.mime_type,
        "original_name": fm.original_name,
        "size_bytes": fm.size_bytes,
    }


def _post_from_queue_payload(item: SpaceModerationItem) -> Post:
    """Rebuild a :class:`Post` from a moderation-queue payload.

    Kept in sync with the shape we serialise in :meth:`SpaceService.create_post`
    when ``decision == "queue"``. Any change to that shape must be mirrored
    here or approved items lose fields in round-trip.
    """
    payload = item.payload
    raw_fm = payload.get("file_meta")
    file_meta: FileMeta | None = None
    if raw_fm:
        try:
            file_meta = FileMeta(
                url=str(raw_fm.get("url", "")),
                mime_type=str(raw_fm.get("mime_type", "")),
                original_name=str(raw_fm.get("original_name", "")),
                size_bytes=int(raw_fm.get("size_bytes", 0)),
            )
        except TypeError, ValueError:
            file_meta = None
    return Post(
        id=str(payload.get("post_id") or uuid.uuid4().hex),
        author=item.submitted_by,
        type=_coerce_post_type(str(payload.get("type") or "text")),
        created_at=item.submitted_at,
        content=payload.get("content"),
        media_url=payload.get("media_url"),
        file_meta=file_meta,
    )


def _normalise_exempt_types(
    value: tuple[str, ...] | list[str] | None,
) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(str(t).strip() for t in value if str(t).strip())
