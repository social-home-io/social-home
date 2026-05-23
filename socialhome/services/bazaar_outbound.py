"""Outbound federation bridge for bazaar listings.

Subscribes to :class:`BazaarListingCreated` and broadcasts the matching
:data:`FederationEventType.BAZAAR_LISTING_CREATED` to every household
that has a member in the listing's space.

The wrapper ``PostType.BAZAAR`` post federates via :class:`SpacePostOutbound`
already, carrying only the caption text. Without this service the remote
member sees ``🛍 Title`` and nothing else — no mode, no price, no photos,
no status. This module ships the actual :class:`BazaarListing` payload so
the remote SPA can render the listing card the same way the originating
household does.

Image bytes hand off to :class:`SpaceMediaSyncService` after the metadata
broadcast — the same outbox the post + gallery paths use. The
``correlation_id`` for a bazaar blob is ``listing.post_id`` so a future
re-enqueue (catch-up, retry) hits the same primary key.

Sender-side gating: the broadcast respects
:data:`FederationCapability.MIN_FOR_BAZAAR_LISTING`. Sub-v_10 peers
silently see only the wrapper post (today's behaviour) instead of partial
data with missing fields.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import BazaarListingCreated
from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.bazaar_repo import AbstractBazaarRepo
    from .space_media_sync_service import SpaceMediaSyncService

log = logging.getLogger(__name__)


class BazaarOutbound:
    """Bus-event → federation broadcaster for bazaar listings."""

    __slots__ = (
        "_bus",
        "_federation",
        "_bazaar_repo",
        "_media_sync",
        "_federation_repo",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: "FederationService",
        bazaar_repo: "AbstractBazaarRepo",
        media_sync: "SpaceMediaSyncService | None" = None,
        federation_repo=None,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._bazaar_repo = bazaar_repo
        self._media_sync = media_sync
        self._federation_repo = federation_repo
        self._bus.subscribe(BazaarListingCreated, self._on_listing_created)

    async def _on_listing_created(self, event: BazaarListingCreated) -> None:
        """Fan ``BAZAAR_LISTING_CREATED`` to every member household.

        The bus event only carries IDs — fetch the full row from the
        repo so the payload mirrors the receiver-side expectations
        for a complete listing reconstruction.
        """
        listing = await self._bazaar_repo.get_listing(event.listing_post_id)
        if listing is None:
            log.warning(
                "BAZAAR_LISTING_CREATED: listing %s vanished before "
                "broadcast — skipping",
                event.listing_post_id,
            )
            return
        payload: dict = {
            "post_id": listing.post_id,
            "space_id": listing.space_id,
            "seller_user_id": listing.seller_user_id,
            "mode": listing.mode.value,
            "title": listing.title,
            "description": listing.description,
            "image_urls": list(listing.image_urls),
            "end_time": listing.end_time,
            "currency": listing.currency,
            "status": listing.status.value,
            "price": listing.price,
            "start_price": listing.start_price,
            "step_price": listing.step_price,
            "created_at": listing.created_at,
        }
        try:
            await self._federation.broadcast_to_space_members(
                listing.space_id,
                FederationEventType.BAZAAR_LISTING_CREATED,
                payload,
                min_proto_version=FederationCapability.MIN_FOR_BAZAAR_LISTING,
            )
        except Exception:
            log.exception(
                "BAZAAR_LISTING_CREATED broadcast failed for space=%s listing=%s",
                listing.space_id,
                listing.post_id,
            )
        # Hand off image bytes to the shared media outbox. Same pattern
        # as SpacePostOutbound: enqueue per-peer rows so the scheduler
        # can chunk + retry independently of the metadata broadcast.
        if (
            self._media_sync is not None
            and self._federation_repo is not None
            and listing.image_urls
        ):
            try:
                targets = await self._federation_repo.list_member_instance_ids(
                    listing.space_id,
                )
            except Exception:
                log.exception(
                    "BAZAAR media enqueue: list peers failed for space=%s listing=%s",
                    listing.space_id,
                    listing.post_id,
                )
                return
            own = getattr(self._federation, "_own_instance_id", "") or ""
            targets = [t for t in targets if t and t != own]
            if targets:
                try:
                    await self._media_sync.enqueue_for_blob(
                        space_id=listing.space_id,
                        correlation_id=listing.post_id,
                        target_instance_ids=targets,
                        media_urls=list(listing.image_urls),
                    )
                except Exception:
                    log.exception(
                        "BAZAAR media enqueue failed for space=%s listing=%s",
                        listing.space_id,
                        listing.post_id,
                    )
