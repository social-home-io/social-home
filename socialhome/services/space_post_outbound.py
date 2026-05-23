"""Outbound federation bridge for space-feed posts.

Subscribes to :class:`SpacePostCreated` / :class:`PostEdited` /
:class:`PostDeleted` (when scoped to a space) and broadcasts the
corresponding ``SPACE_POST_*`` federation event to every household
that has a member in the space — direct delivery when the peer is
paired CONFIRMED, mesh-routed under ``SPACE_ROUTED`` when not.

Before this service, ``SpacePostCreated`` was only consumed by
local bridges (HA, realtime WS, search indexer, system-album); the
federation outbound for the post never fired, so a remote member
on another household never saw posts in spaces they belonged to.
:mod:`socialhome.services.federation_inbound_service` already
had the inbound half wired (``_on_space_post_created`` saves to
``space_posts`` on receive); the missing outbound is what this
module supplies.

Membership-gating is handled by
:meth:`FederationService.broadcast_to_space_members` — it queries
``space_instances`` for the broadcast set, so non-member relays
never receive the envelope directly. Mesh-routed delivery to a
non-paired member wraps in ``SPACE_ROUTED`` so the intermediate
relay sees only opaque ciphertext (§D1b transport rule, see
CLAUDE.md "Encryption-First Rule").
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import (
    CommentAdded,
    CommentDeleted,
    CommentUpdated,
    PostDeleted,
    PostEdited,
    SpacePostCreated,
)
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService

log = logging.getLogger(__name__)


class SpacePostOutbound:
    """Bus-event → federation broadcaster for space posts."""

    __slots__ = ("_bus", "_federation")

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: "FederationService",
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._bus.subscribe(SpacePostCreated, self._on_space_post_created)
        self._bus.subscribe(PostEdited, self._on_post_edited)
        self._bus.subscribe(PostDeleted, self._on_post_deleted)
        # #117 followup — same gap on comments. ``CommentAdded`` /
        # ``CommentUpdated`` / ``CommentDeleted`` already carry
        # ``space_id`` (unlike the post-level events), so we can
        # gate-and-federate cleanly here.
        self._bus.subscribe(CommentAdded, self._on_comment_added)
        self._bus.subscribe(CommentUpdated, self._on_comment_updated)
        self._bus.subscribe(CommentDeleted, self._on_comment_deleted)

    async def _on_space_post_created(self, event: SpacePostCreated) -> None:
        """Fan ``SPACE_POST_CREATED`` to every member household.

        The payload mirrors what
        :meth:`FederationInboundService._post_from_payload` expects —
        adding fields here without a matching receiver-side change
        is harmless (older peers ignore extras).
        """
        post = event.post
        # Local source-of-truth fires bus events for both household
        # feed posts (no space_id) AND space posts. Federation here is
        # only for space-scoped rows.
        if not event.space_id:
            return
        # ``origin_instance_id`` is None when the bus event came from
        # a local POST (the SPA's ``/api/spaces/{id}/posts``);
        # populated when ``federation_inbound_service`` re-published
        # after receiving SPACE_POST_CREATED. Re-broadcasting an
        # inbound-driven event would create a federation loop:
        # peer → us → peer → us → … Skip the publish.
        if event.origin_instance_id is not None:
            return
        payload: dict = {
            "id": post.id,
            "space_id": event.space_id,
            "author": post.author,
            "type": post.type.value,
            "content": post.content,
            "media_url": post.media_url,
            "image_urls": list(post.image_urls),
            "occurred_at": post.created_at.isoformat() if post.created_at else None,
        }
        if post.location is not None:
            payload["location"] = {
                "lat": post.location.lat,
                "lon": post.location.lon,
                "label": post.location.label,
            }
        if post.file_meta is not None:
            payload["file_meta"] = {
                "url": post.file_meta.url,
                "mime_type": post.file_meta.mime_type,
                "original_name": post.file_meta.original_name,
                "size_bytes": post.file_meta.size_bytes,
            }
        try:
            await self._federation.broadcast_to_space_members(
                event.space_id,
                FederationEventType.SPACE_POST_CREATED,
                payload,
            )
        except Exception:
            log.exception(
                "SPACE_POST_CREATED broadcast failed for space=%s post=%s",
                event.space_id,
                post.id,
            )

    async def _on_post_edited(self, event: PostEdited) -> None:
        # ``PostEdited`` is fired for both household and space posts;
        # the bus event doesn't currently carry a space_id, so we
        # can't safely federate this without re-looking-up the post.
        # Out of scope for the first cut — household-only edits stay
        # local, space-edit federation lands in a followup.
        return

    async def _on_post_deleted(self, event: PostDeleted) -> None:
        # Same caveat as edit — needs space_id resolution.
        return

    # ── Comments ─────────────────────────────────────────────────────────

    async def _on_comment_added(self, event: CommentAdded) -> None:
        if not event.space_id:
            return
        if event.origin_instance_id is not None:
            return
        c = event.comment
        payload: dict = {
            "id": c.id,
            "comment_id": c.id,
            "post_id": event.post_id,
            "space_id": event.space_id,
            "author": c.author,
            "type": c.type.value,
            "content": c.content,
            "media_url": c.media_url,
            "parent_id": c.parent_id,
            "occurred_at": c.created_at.isoformat() if c.created_at else None,
        }
        try:
            await self._federation.broadcast_to_space_members(
                event.space_id,
                FederationEventType.SPACE_COMMENT_CREATED,
                payload,
            )
        except Exception:
            log.exception(
                "SPACE_COMMENT_CREATED broadcast failed for space=%s comment=%s",
                event.space_id,
                c.id,
            )

    async def _on_comment_updated(self, event: CommentUpdated) -> None:
        if not event.space_id:
            return
        if event.origin_instance_id is not None:
            return
        c = event.comment
        payload: dict = {
            "id": c.id,
            "comment_id": c.id,
            "post_id": event.post_id,
            "space_id": event.space_id,
            "content": c.content,
        }
        try:
            await self._federation.broadcast_to_space_members(
                event.space_id,
                FederationEventType.SPACE_COMMENT_UPDATED,
                payload,
            )
        except Exception:
            log.exception(
                "SPACE_COMMENT_UPDATED broadcast failed for space=%s comment=%s",
                event.space_id,
                c.id,
            )

    async def _on_comment_deleted(self, event: CommentDeleted) -> None:
        if not event.space_id:
            return
        if event.origin_instance_id is not None:
            return
        payload = {
            "comment_id": event.comment_id,
            "id": event.comment_id,
            "post_id": event.post_id,
            "space_id": event.space_id,
        }
        try:
            await self._federation.broadcast_to_space_members(
                event.space_id,
                FederationEventType.SPACE_COMMENT_DELETED,
                payload,
            )
        except Exception:
            log.exception(
                "SPACE_COMMENT_DELETED broadcast failed for space=%s comment=%s",
                event.space_id,
                event.comment_id,
            )
