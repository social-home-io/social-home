"""Bazaar exporter — every listing for a space.

The wrapper ``PostType.BAZAAR`` post already streams under the
``posts`` resource; this exporter ships the matching
``BazaarListing`` rows so the receiver can render the full listing
card (mode / price / photos / status). Image bytes ride the
existing :class:`SpaceMediaSyncService` outbox — see
:meth:`SpaceSyncService._enqueue_catchup_media` for the enqueue.

Without this exporter (#445 shipped realtime + image bytes only),
a new joiner saw the wrapper post but the ``bazaar_listings`` row
stayed empty until the seller fired a new ``BAZAAR_LISTING_CREATED``
event — broken cards for every pre-existing listing.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .....repositories.bazaar_repo import AbstractBazaarRepo


class BazaarExporter:
    resource = "bazaar"

    __slots__ = ("_repo",)

    def __init__(self, bazaar_repo: "AbstractBazaarRepo") -> None:
        self._repo = bazaar_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        listings = await self._repo.list_in_space(space_id, limit=2000)
        out: list[dict[str, Any]] = []
        for lst in listings:
            out.append(
                {
                    "post_id": lst.post_id,
                    "space_id": lst.space_id,
                    "seller_user_id": lst.seller_user_id,
                    "mode": lst.mode.value,
                    "title": lst.title,
                    "description": lst.description,
                    "image_urls": list(lst.image_urls),
                    "end_time": lst.end_time,
                    "currency": lst.currency,
                    "status": lst.status.value,
                    "price": lst.price,
                    "start_price": lst.start_price,
                    "step_price": lst.step_price,
                    "winner_user_id": lst.winner_user_id,
                    "winning_price": lst.winning_price,
                    "sold_at": lst.sold_at,
                    "created_at": lst.created_at,
                },
            )
        return out
