"""Shopping list repository (§23.120).

Household-scoped. Two tables:

* ``shopping_list_items`` — the actual list. Items optionally carry a
  ``store`` (free-form name of the shop where the item should be
  bought).
* ``shopping_stores`` — the household's store catalogue, the only
  table that durably remembers a store's "trip order". Auto-upserted
  on first sighting from an item's ``store`` field so the catalogue
  grows organically; rows are NOT removed when items that referenced
  them go away, so a family keeps its drag-defined order across empty
  shopping lists.

The service layer is responsible for cleaning up completed items on
a schedule or on user action.
"""

from __future__ import annotations

import builtins
import uuid
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from .base import bool_col, row_to_dict, rows_to_dicts


# Domain dataclasses live in ``socialhome/domain/shopping.py``;
# re-exported here so existing repo-level imports keep working.
from ..domain.shopping import ShoppingItem, ShoppingStore  # noqa: F401,E402


# The repo has a method named ``list`` which shadows the builtin
# inside class scope under mypy's strict resolution. Use this alias
# for any ``list[...]`` annotation declared in the class body.
_list = builtins.list


#: Sentinel for :meth:`SqliteShoppingRepo.update_item` so the route
#: layer can express "leave this field alone" alongside "clear" /
#: "set to value". ``None`` already means "clear" for ``store``; we
#: need a third state to mean "no change" without overloading the
#: ``None`` slot. Same shape as ``UNSET_COVER`` / ``UNSET_LOCATION``
#: in the calendar service.
_UNSET: object = object()
UNSET_FIELD: object = _UNSET


@runtime_checkable
class AbstractShoppingRepo(Protocol):
    async def add(
        self,
        text: str,
        *,
        created_by: str,
        store: str | None = None,
    ) -> ShoppingItem: ...
    async def get(self, item_id: str) -> ShoppingItem | None: ...
    async def list(self, *, include_completed: bool = False) -> _list[ShoppingItem]: ...
    async def update_item(
        self,
        item_id: str,
        *,
        text: str | None = None,
        store: object = _UNSET,
    ) -> ShoppingItem | None: ...
    async def complete(self, item_id: str) -> None: ...
    async def uncomplete(self, item_id: str) -> None: ...
    async def delete(self, item_id: str) -> None: ...
    async def clear_completed(self) -> int: ...
    async def list_stores(self) -> _list[ShoppingStore]: ...
    async def touch_store(self, name: str) -> None: ...
    async def reorder_stores(self, ordered_names: _list[str]) -> None: ...


class SqliteShoppingRepo:
    """SQLite-backed :class:`AbstractShoppingRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ─── Items ───────────────────────────────────────────────────────────

    async def add(
        self,
        text: str,
        *,
        created_by: str,
        store: str | None = None,
    ) -> ShoppingItem:
        text = text.strip()
        if not text:
            raise ValueError("shopping item text must not be empty")
        now = datetime.now(timezone.utc).isoformat()
        item = ShoppingItem(
            id=uuid.uuid4().hex,
            text=text,
            completed=False,
            created_by=created_by,
            created_at=now,
            completed_at=None,
            store=store,
        )
        await self._db.enqueue(
            """
            INSERT INTO shopping_list_items(
                id, text, completed, created_by, created_at,
                completed_at, store
            ) VALUES(?, ?, 0, ?, ?, NULL, ?)
            """,
            (item.id, item.text, item.created_by, item.created_at, item.store),
        )
        if store:
            await self.touch_store(store)
        return item

    async def get(self, item_id: str) -> ShoppingItem | None:
        row = await self._db.fetchone(
            "SELECT * FROM shopping_list_items WHERE id=?",
            (item_id,),
        )
        return _row_to_item(row_to_dict(row))

    async def list(
        self,
        *,
        include_completed: bool = False,
    ) -> _list[ShoppingItem]:
        if include_completed:
            rows = await self._db.fetchall(
                "SELECT * FROM shopping_list_items ORDER BY completed ASC, created_at",
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM shopping_list_items WHERE completed=0 "
                "ORDER BY created_at",
            )
        return [i for i in (_row_to_item(d) for d in rows_to_dicts(rows)) if i]

    async def update_item(
        self,
        item_id: str,
        *,
        text: str | None = None,
        store: object = _UNSET,
    ) -> ShoppingItem | None:
        """Patch a single item's text / store. Returns the row after the
        write so the service layer can publish a fresh-state event
        without a second round-trip.

        ``text=None`` means "leave unchanged" — empty strings are
        rejected by the service layer before reaching here. ``store``
        uses the :data:`_UNSET` sentinel because ``None`` is a
        meaningful value (clear the field).
        """
        existing = await self.get(item_id)
        if existing is None:
            return None

        new_text = text if text is not None else existing.text
        if store is _UNSET:
            new_store = existing.store
        else:
            assert store is None or isinstance(store, str)
            new_store = store

        await self._db.enqueue(
            "UPDATE shopping_list_items SET text=?, store=? WHERE id=?",
            (new_text, new_store, item_id),
        )
        if new_store:
            await self.touch_store(new_store)
        return ShoppingItem(
            id=existing.id,
            text=new_text,
            completed=existing.completed,
            created_by=existing.created_by,
            created_at=existing.created_at,
            completed_at=existing.completed_at,
            store=new_store,
        )

    async def complete(self, item_id: str) -> None:
        await self._db.enqueue(
            """
            UPDATE shopping_list_items
               SET completed=1, completed_at=datetime('now')
             WHERE id=? AND completed=0
            """,
            (item_id,),
        )

    async def uncomplete(self, item_id: str) -> None:
        await self._db.enqueue(
            """
            UPDATE shopping_list_items
               SET completed=0, completed_at=NULL
             WHERE id=?
            """,
            (item_id,),
        )

    async def delete(self, item_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM shopping_list_items WHERE id=?",
            (item_id,),
        )

    async def clear_completed(self) -> int:
        """Delete every completed item. Returns the count removed."""
        count = await self._db.fetchval(
            "SELECT COUNT(*) FROM shopping_list_items WHERE completed=1",
            default=0,
        )
        await self._db.enqueue(
            "DELETE FROM shopping_list_items WHERE completed=1",
        )
        return int(count)

    # ─── Stores ──────────────────────────────────────────────────────────

    async def list_stores(self) -> _list[ShoppingStore]:
        rows = await self._db.fetchall(
            "SELECT name, sort_order FROM shopping_stores ORDER BY sort_order, name",
        )
        return [
            ShoppingStore(name=d["name"], sort_order=int(d["sort_order"]))
            for d in rows_to_dicts(rows)
        ]

    async def touch_store(self, name: str) -> None:
        """Ensure ``name`` exists in the catalogue, appending it past
        the current max if new. Idempotent — a second call with the
        same name leaves the existing ``sort_order`` alone, which is
        what we want for autosuggest re-typing.
        """
        if not name:
            return
        max_order = await self._db.fetchval(
            "SELECT COALESCE(MAX(sort_order), -1) FROM shopping_stores",
            default=-1,
        )
        next_order = int(max_order) + 1
        await self._db.enqueue(
            """
            INSERT INTO shopping_stores(name, sort_order) VALUES(?, ?)
            ON CONFLICT(name) DO NOTHING
            """,
            (name, next_order),
        )

    async def reorder_stores(self, ordered_names: _list[str]) -> None:
        """Assign ``sort_order = index`` to each name in ``ordered_names``.

        Names not in the input keep their relative order but shift
        past the end (their ``sort_order`` is bumped to
        ``len(ordered_names) + i``). Unknown names in the input are
        ignored — the user might be reordering on stale data.
        """
        current = await self.list_stores()
        current_names = [s.name for s in current]
        known = set(current_names)
        seen: set[str] = set()
        new_order: _list[str] = []
        for name in ordered_names:
            if name in known and name not in seen:
                seen.add(name)
                new_order.append(name)
        # Any catalogue rows the input forgot keep their relative
        # order tucked behind the explicitly-ordered tail.
        for name in current_names:
            if name not in seen:
                new_order.append(name)
        for idx, name in enumerate(new_order):
            await self._db.enqueue(
                "UPDATE shopping_stores SET sort_order=? WHERE name=?",
                (idx, name),
            )


def _row_to_item(row: dict | None) -> ShoppingItem | None:
    if row is None:
        return None
    return ShoppingItem(
        id=row["id"],
        text=row["text"],
        completed=bool_col(row.get("completed", 0)),
        created_by=row["created_by"],
        created_at=row["created_at"],
        completed_at=row.get("completed_at"),
        store=row.get("store"),
    )
