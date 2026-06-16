"""Outbound federation for user profile updates (§4 / §23 profile).

Subscribes to :class:`UserProfileUpdated` and fans the event out as a
``USER_UPDATED`` payload to every paired instance that has a live
mirror of this user (a row in ``remote_users`` on the peer).

The payload always carries display name + bio + picture hash. When the
picture bytes changed in this publication, they travel as a base64
``picture_webp_base64`` field so the peer can re-validate + store
locally. When the bytes are unchanged, the field is omitted (hash alone
is enough to cache-bust URLs built from the prior bytes).

Household-scope only: paired peers receive updates for users whose
``user_id`` appears in their local ``remote_users`` — the inbound
handler treats any unknown ``user_id`` as an upsert-anyway which is
the documented §24 behaviour.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

from ..domain.events import UserProfileUpdated
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus
from .peer_outbound import ConfirmedPeerBroadcaster, SingleTargetSender
from .user_identity_binding import user_identity_binding_fields
from .visibility import VisibilityMixin

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.peer_user_visibility_repo import (
        AbstractPeerUserVisibilityRepo,
    )
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)


class ProfileFederationOutbound(
    VisibilityMixin,
    ConfirmedPeerBroadcaster,
    SingleTargetSender,
):
    """Publish :class:`UserProfileUpdated` events as ``USER_UPDATED``."""

    # Narrow the mixin's ``FederationService | None`` slot — this service
    # always wires a live federation service in ``__init__``.
    _federation: "FederationService"

    __slots__ = (
        "_bus",
        "_federation",
        "_federation_repo",
        "_user_repo",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: "FederationService",
        federation_repo: "AbstractFederationRepo",
        visibility_repo: "AbstractPeerUserVisibilityRepo | None" = None,
        user_repo: "AbstractUserRepo | None" = None,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._federation_repo = federation_repo
        # Optional so existing test wiring (which constructs this
        # service without the visibility filter) keeps working — when
        # unset, every user fans to every confirmed peer just like
        # before this PR.
        self._visibility_repo = visibility_repo
        # Optional — supplies the per-user Ed25519 identity keypair for the
        # proto-v_25 identity binding. When unset the binding is simply
        # omitted (legacy shape) even for a v_25 peer.
        self._user_repo = user_repo

    def wire(self) -> None:
        self._bus.subscribe(UserProfileUpdated, self._on_updated)

    async def _on_updated(self, event: UserProfileUpdated) -> None:
        # The public @handle rides USER_UPDATED as unsigned display metadata,
        # exactly like ``display_name``. Most ``UserProfileUpdated`` publishers
        # (display_name/bio/picture edits, renames) leave ``event.handle`` None
        # even when the user has a handle, so back-fill it from the user row —
        # otherwise a non-handle edit would clobber the peer's cached handle.
        handle = event.handle
        if handle is None and self._user_repo is not None:
            user = await self._user_repo.get_by_user_id(event.user_id)
            if user is not None:
                handle = user.handle
        payload: dict = {
            "user_id": event.user_id,
            "username": event.username,
            "display_name": event.display_name,
            "bio": event.bio,
            "picture_hash": event.picture_hash,
            "handle": handle,
        }
        if event.picture_webp is not None:
            payload["picture_webp_base64"] = base64.b64encode(
                event.picture_webp,
            ).decode("ascii")

        for instance_id in await self.list_confirmed_peer_ids():
            # Per-pair user-visibility filter (peer_user_visibility).
            # ``hidden_for_peer`` is fail-soft — repo errors default to
            # visible so we never silently lose profile updates on a
            # transient infra blip.
            hidden = await self.hidden_for_peer(instance_id)
            if event.user_id in hidden:
                continue
            # Per-user identity binding (proto v_25). Computed per-peer
            # because the v_25 gate is per-peer; a v_24 peer keeps the
            # legacy payload untouched.
            binding = await user_identity_binding_fields(
                federation_service=self._federation,
                user_repo=self._user_repo,
                peer_instance_id=instance_id,
                user_id=event.user_id,
                username=event.username,
                display_name=event.display_name,
                picture_hash=event.picture_hash,
            )
            await self.send_to_instance(
                instance_id,
                FederationEventType.USER_UPDATED,
                {**payload, **binding} if binding else payload,
            )
