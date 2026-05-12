"""Shopping-list domain types (§17)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ShoppingItem:
    """One entry on the household shopping list.

    ``store`` is the free-form name of the store / shop where the
    item should be bought (e.g. ``"Aldi"``, ``"Bakery"``). It mirrors
    a row in :class:`ShoppingStore` — the catalogue is what carries
    the household-defined trip order. ``None`` means "unassigned";
    the SPA renders those under a trailing "No store" group.
    """

    id: str
    text: str
    completed: bool
    created_by: str  # user_id
    created_at: str
    completed_at: str | None = None
    store: str | None = None


@dataclass(slots=True, frozen=True)
class ShoppingStore:
    """One entry in the household's shopping-store catalogue.

    Rows are auto-upserted by the repo on first reference from
    :class:`ShoppingItem.store` so the catalogue grows organically.
    The durable state is ``sort_order`` — the index the household
    has dragged this store to in their "trip order". The catalogue
    persists even when every item that referenced the store is
    gone, so a family doesn't have to re-sort after each shop.
    """

    name: str
    sort_order: int
