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
from ._visibility import hidden_for_peer

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.peer_user_visibility_repo import (
        AbstractPeerUserVisibilityRepo,
    )

log = logging.getLogger(__name__)


class ProfileFederationOutbound:
    """Publish :class:`UserProfileUpdated` events as ``USER_UPDATED``."""

    __slots__ = (
        "_bus",
        "_federation",
        "_federation_repo",
        "_visibility_repo",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: "FederationService",
        federation_repo: "AbstractFederationRepo",
        visibility_repo: "AbstractPeerUserVisibilityRepo | None" = None,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._federation_repo = federation_repo
        # Optional so existing test wiring (which constructs this
        # service without the visibility filter) keeps working — when
        # unset, every user fans to every confirmed peer just like
        # before this PR.
        self._visibility_repo = visibility_repo

    def wire(self) -> None:
        self._bus.subscribe(UserProfileUpdated, self._on_updated)

    async def _on_updated(self, event: UserProfileUpdated) -> None:
        payload: dict = {
            "user_id": event.user_id,
            "username": event.username,
            "display_name": event.display_name,
            "bio": event.bio,
            "picture_hash": event.picture_hash,
        }
        if event.picture_webp is not None:
            payload["picture_webp_base64"] = base64.b64encode(
                event.picture_webp,
            ).decode("ascii")

        try:
            peers = await self._federation_repo.list_instances(
                status="confirmed",
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("profile-outbound: list peers failed: %s", exc)
            return
        own = getattr(self._federation, "_own_instance_id", "")
        for peer in peers:
            instance_id = getattr(peer, "id", None)
            if not instance_id or instance_id == own:
                continue
            # Per-pair user-visibility filter (peer_user_visibility).
            # ``hidden_for_peer`` is fail-soft — repo errors default to
            # visible so we never silently lose profile updates on a
            # transient infra blip.
            hidden = await hidden_for_peer(
                self._visibility_repo,
                instance_id,
            )
            if event.user_id in hidden:
                continue
            try:
                await self._federation.send_event(
                    to_instance_id=instance_id,
                    event_type=FederationEventType.USER_UPDATED,
                    payload=payload,
                )
            except Exception as exc:  # pragma: no cover — defensive
                log.debug(
                    "profile-outbound: send to %s failed: %s",
                    instance_id,
                    exc,
                )
