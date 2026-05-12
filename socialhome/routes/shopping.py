"""Shopping list routes — /api/shopping/* (§23.120)."""

from __future__ import annotations

from aiohttp import web

from ..app_keys import shopping_service_key
from ..repositories.shopping_repo import UNSET_FIELD
from ..security import error_response
from .base import BaseView


def _item_dict(item) -> dict:
    """Wire-shape for a :class:`ShoppingItem`. ``store`` is always
    present (``None`` when unassigned) so the SPA can patch a stale
    cache entry without a re-fetch."""
    return {
        "id": item.id,
        "text": item.text,
        "completed": item.completed,
        "created_by": item.created_by,
        "created_at": item.created_at,
        "completed_at": item.completed_at,
        "store": item.store,
    }


class ShoppingCollectionView(BaseView):
    """``GET /api/shopping`` + ``POST /api/shopping``."""

    async def get(self) -> web.Response:
        self.user
        include_completed = (
            self.request.query.get("include_completed", "false").lower() == "true"
        )
        items = await self.svc(shopping_service_key).list_items(
            include_completed=include_completed,
        )
        return self._json([_item_dict(i) for i in items])

    async def post(self) -> web.Response:
        ctx = self.user
        body = await self.body()
        item = await self.svc(shopping_service_key).add_item(
            body.get("text", ""),
            created_by=ctx.user_id,
            store=body.get("store"),
        )
        return self._json(_item_dict(item), status=201)


class ShoppingItemDetailView(BaseView):
    """``PATCH`` / ``DELETE /api/shopping/{id}``."""

    async def patch(self) -> web.Response:
        """Edit ``text`` and / or ``store`` on an existing item.

        Tri-state on ``store``:

        * key omitted in body → keep the existing value;
        * ``store: null`` → clear the field;
        * ``store: "Aldi"`` → set the field.
        """
        self.user
        item_id = self.match("id")
        body = await self.body()
        store = body["store"] if "store" in body else UNSET_FIELD
        item = await self.svc(shopping_service_key).update_item(
            item_id,
            text=body.get("text"),
            store=store,
        )
        return self._json(_item_dict(item))

    async def delete(self) -> web.Response:
        self.user
        item_id = self.match("id")
        await self.svc(shopping_service_key).delete_item(item_id)
        return self._json({"ok": True})


class ShoppingItemCompleteView(BaseView):
    """``PATCH /api/shopping/{id}/complete``."""

    async def patch(self) -> web.Response:
        self.user
        item_id = self.match("id")
        await self.svc(shopping_service_key).complete_item(item_id)
        return self._json({"ok": True})


class ShoppingItemUncompleteView(BaseView):
    """``PATCH /api/shopping/{id}/uncomplete``."""

    async def patch(self) -> web.Response:
        self.user
        item_id = self.match("id")
        await self.svc(shopping_service_key).uncomplete_item(item_id)
        return self._json({"ok": True})


class ShoppingClearCompletedView(BaseView):
    """``POST /api/shopping/clear-completed``."""

    async def post(self) -> web.Response:
        self.user
        count = await self.svc(shopping_service_key).clear_completed()
        return self._json({"cleared": count})


class ShoppingStoresView(BaseView):
    """``GET /api/shopping/stores`` — return the household catalogue
    in canonical ``sort_order``."""

    async def get(self) -> web.Response:
        self.user
        stores = await self.svc(shopping_service_key).list_stores()
        return self._json(
            [{"name": s.name, "sort_order": s.sort_order} for s in stores]
        )


class ShoppingStoresOrderView(BaseView):
    """``PUT /api/shopping/stores/order`` — replace the household's
    drag-defined trip order.

    Body: ``{"order": ["Bakery", "Aldi", "Whole Foods"]}``. Unknown
    names are ignored (the SPA might be reordering on stale data);
    catalogue names that are missing from ``order`` retain their
    relative order but shift past the explicitly-ordered tail.
    """

    async def put(self) -> web.Response:
        self.user
        body = await self.body()
        order = body.get("order") or []
        if not isinstance(order, list):
            return error_response(
                422,
                "UNPROCESSABLE",
                "`order` must be an array of store names.",
            )
        stores = await self.svc(shopping_service_key).reorder_stores(
            [str(n) for n in order],
        )
        return self._json(
            [{"name": s.name, "sort_order": s.sort_order} for s in stores]
        )
