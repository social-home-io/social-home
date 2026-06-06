"""Tests for SqliteShoppingRepo — shopping list CRUD."""

from __future__ import annotations

import pytest

from socialhome.repositories.shopping_repo import SqliteShoppingRepo


@pytest.fixture
async def env(tmp_dir):
    """Env with a shopping repo over a real SQLite database."""
    from socialhome.crypto import generate_identity_keypair, derive_instance_id
    from socialhome.db.database import AsyncDatabase

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class E:
        pass

    e = E()
    e.db = db
    e.repo = SqliteShoppingRepo(db)
    yield e
    await db.shutdown()


async def test_add_and_get_item(env):
    """add creates a shopping item; get retrieves it by id."""
    item = await env.repo.add("Milk", created_by="uid-alice")
    assert item.text == "Milk"
    assert item.completed is False
    fetched = await env.repo.get(item.id)
    assert fetched is not None
    assert fetched.text == "Milk"


async def test_add_empty_text_raises(env):
    """add raises ValueError when text is empty or whitespace."""
    with pytest.raises(ValueError, match="must not be empty"):
        await env.repo.add("   ", created_by="uid-alice")


async def test_get_missing_returns_none(env):
    """get returns None for an unknown item id."""
    assert await env.repo.get("no-such-id") is None


async def test_list_excludes_completed_by_default(env):
    """list() without include_completed only returns pending items."""
    item1 = await env.repo.add("Eggs", created_by="uid-alice")
    item2 = await env.repo.add("Butter", created_by="uid-alice")
    await env.repo.complete(item1.id)
    result = await env.repo.list()
    ids = [i.id for i in result]
    assert item1.id not in ids
    assert item2.id in ids


async def test_list_with_completed(env):
    """list(include_completed=True) returns both pending and completed items."""
    item = await env.repo.add("Sugar", created_by="uid-alice")
    await env.repo.complete(item.id)
    result = await env.repo.list(include_completed=True)
    assert any(i.id == item.id for i in result)


async def test_complete_and_uncomplete(env):
    """complete marks an item done; uncomplete reverses it."""
    item = await env.repo.add("Cheese", created_by="uid-alice")
    await env.repo.complete(item.id)
    fetched = await env.repo.get(item.id)
    assert fetched.completed is True
    await env.repo.uncomplete(item.id)
    fetched2 = await env.repo.get(item.id)
    assert fetched2.completed is False


async def test_delete_item(env):
    """delete removes the item from the list."""
    item = await env.repo.add("Bread", created_by="uid-alice")
    await env.repo.delete(item.id)
    assert await env.repo.get(item.id) is None


async def test_clear_completed(env):
    """clear_completed removes all completed items and returns the count."""
    i1 = await env.repo.add("A", created_by="uid-alice")
    i2 = await env.repo.add("B", created_by="uid-alice")
    i3 = await env.repo.add("C", created_by="uid-alice")
    await env.repo.complete(i1.id)
    await env.repo.complete(i2.id)
    cleared = await env.repo.clear_completed()
    assert cleared == 2
    # Uncompleted item remains
    assert await env.repo.get(i3.id) is not None
    # Completed items are gone
    assert await env.repo.get(i1.id) is None


# ─── Store column + catalogue ─────────────────────────────────────────────


async def test_add_with_store_persists_and_creates_catalogue_row(env):
    """add(store=…) persists the field AND auto-upserts a catalogue row."""
    item = await env.repo.add("Milk", created_by="uid-alice", store="Aldi")
    assert item.store == "Aldi"

    fetched = await env.repo.get(item.id)
    assert fetched.store == "Aldi"

    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Aldi"]
    assert stores[0].sort_order == 0


async def test_add_without_store_leaves_catalogue_empty(env):
    """Plain add (no store) does NOT seed the catalogue."""
    await env.repo.add("Eggs", created_by="uid-alice")
    assert await env.repo.list_stores() == []


async def test_touch_store_assigns_increasing_sort_order(env):
    """Each new store gets MAX(sort_order)+1; re-touching is idempotent."""
    await env.repo.touch_store("Aldi")
    await env.repo.touch_store("Bakery")
    await env.repo.touch_store("Whole Foods")
    # Idempotent — re-touching Aldi MUST NOT bump its order.
    await env.repo.touch_store("Aldi")

    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Aldi", "Bakery", "Whole Foods"]
    assert [s.sort_order for s in stores] == [0, 1, 2]


async def test_touch_store_ignores_empty(env):
    """Empty / whitespace-only names don't create catalogue rows."""
    await env.repo.touch_store("")
    assert await env.repo.list_stores() == []


async def test_add_store_matches_existing_case_insensitively(env):
    """Adding ``@ aldi`` when ``Aldi`` already exists reuses the
    existing catalogue casing instead of spawning a duplicate row —
    and the item carries the canonical name so grouping stays merged."""
    await env.repo.add("Milk", created_by="u1", store="Aldi")
    item = await env.repo.add("Eggs", created_by="u1", store="aldi")

    # The item is stored under the catalogue's existing casing.
    assert item.store == "Aldi"
    # No duplicate catalogue row.
    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Aldi"]


async def test_add_new_store_keeps_its_own_casing(env):
    """A genuinely new store keeps exactly the casing the user typed —
    canonicalisation only kicks in against an existing match."""
    item = await env.repo.add("Milk", created_by="u1", store="Whole Foods")
    assert item.store == "Whole Foods"
    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Whole Foods"]


async def test_update_item_store_matches_existing_case_insensitively(env):
    """update_item canonicalises a case-variant store the same way add
    does, so editing an item's store can't fork the catalogue."""
    await env.repo.touch_store("Bakery")
    item = await env.repo.add("Bread", created_by="u1")

    updated = await env.repo.update_item(item.id, store="bakery")

    assert updated.store == "Bakery"
    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Bakery"]


async def test_update_item_text_only_keeps_store(env):
    """update_item(text=…) without store sentinel leaves the store alone."""
    item = await env.repo.add("Milk", created_by="uid-alice", store="Aldi")
    updated = await env.repo.update_item(item.id, text="Whole Milk")
    assert updated.text == "Whole Milk"
    assert updated.store == "Aldi"


async def test_update_item_clears_store_with_none(env):
    """update_item(store=None) clears the field."""
    item = await env.repo.add("Milk", created_by="uid-alice", store="Aldi")
    updated = await env.repo.update_item(item.id, store=None)
    assert updated.store is None
    fetched = await env.repo.get(item.id)
    assert fetched.store is None


async def test_update_item_sets_new_store_and_upserts_catalogue(env):
    """update_item with a brand-new store auto-creates the catalogue row."""
    item = await env.repo.add("Milk", created_by="uid-alice", store="Aldi")
    await env.repo.update_item(item.id, store="Whole Foods")

    stores = await env.repo.list_stores()
    names = [s.name for s in stores]
    assert "Aldi" in names
    assert "Whole Foods" in names
    # Whole Foods was added last, so it gets the tail order.
    whole_foods = next(s for s in stores if s.name == "Whole Foods")
    aldi = next(s for s in stores if s.name == "Aldi")
    assert whole_foods.sort_order > aldi.sort_order


async def test_update_item_unknown_returns_none(env):
    """update_item on an unknown id returns None (route maps to 404)."""
    assert await env.repo.update_item("nope") is None


async def test_reorder_stores_applies_input_order(env):
    """reorder_stores assigns sort_order = index for each named store."""
    await env.repo.touch_store("Aldi")
    await env.repo.touch_store("Bakery")
    await env.repo.touch_store("Whole Foods")

    await env.repo.reorder_stores(["Whole Foods", "Bakery", "Aldi"])

    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Whole Foods", "Bakery", "Aldi"]
    assert [s.sort_order for s in stores] == [0, 1, 2]


async def test_reorder_stores_ignores_unknown_names(env):
    """Unknown names in the input are silently dropped."""
    await env.repo.touch_store("Aldi")
    await env.repo.touch_store("Bakery")

    await env.repo.reorder_stores(["Bakery", "Ghost Store", "Aldi"])

    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Bakery", "Aldi"]


async def test_reorder_stores_keeps_missing_names_past_tail(env):
    """Catalogue rows the input forgot retain their relative order past
    the explicitly-ordered tail."""
    await env.repo.touch_store("Aldi")
    await env.repo.touch_store("Bakery")
    await env.repo.touch_store("Whole Foods")

    await env.repo.reorder_stores(["Whole Foods"])

    stores = await env.repo.list_stores()
    # "Whole Foods" first; the other two keep their original sort
    # (Aldi was touched before Bakery) tucked past it.
    assert [s.name for s in stores] == ["Whole Foods", "Aldi", "Bakery"]
    assert [s.sort_order for s in stores] == [0, 1, 2]


async def test_reorder_stores_dedupes_input(env):
    """A name listed twice in the input is only positioned by its first
    occurrence — defends against a buggy client posting duplicates."""
    await env.repo.touch_store("Aldi")
    await env.repo.touch_store("Bakery")

    await env.repo.reorder_stores(["Aldi", "Bakery", "Aldi"])

    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Aldi", "Bakery"]


async def test_rename_store_updates_catalogue_and_items(env):
    """Renaming cascades to every item whose ``store`` matched."""
    await env.repo.add("Milk", created_by="u1", store="Aldi")
    await env.repo.add("Eggs", created_by="u1", store="Aldi")
    await env.repo.add("Bread", created_by="u1", store="Bakery")

    ok = await env.repo.rename_store("Aldi", "Coop")

    assert ok is True
    stores = await env.repo.list_stores()
    assert "Aldi" not in [s.name for s in stores]
    assert "Coop" in [s.name for s in stores]
    items = await env.repo.list()
    by_text = {i.text: i for i in items}
    assert by_text["Milk"].store == "Coop"
    assert by_text["Eggs"].store == "Coop"
    assert by_text["Bread"].store == "Bakery"


async def test_rename_store_missing_returns_false(env):
    """No-op rename on a non-existent store name — let the route layer map to 404."""
    await env.repo.touch_store("Aldi")
    ok = await env.repo.rename_store("Migrso", "Migros")
    assert ok is False


async def test_rename_store_collision_raises(env):
    """Renaming to an already-taken catalogue name would lose
    items — surface as a ValueError so the route can map to 409."""
    await env.repo.touch_store("Aldi")
    await env.repo.touch_store("Migros")

    import pytest

    with pytest.raises(ValueError):
        await env.repo.rename_store("Aldi", "Migros")


async def test_rename_store_same_name_is_noop(env):
    """Renaming a store to its current name shortcuts without
    touching the DB. Returns True (the store exists)."""
    await env.repo.touch_store("Aldi")
    ok = await env.repo.rename_store("Aldi", "Aldi")
    assert ok is True
    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Aldi"]


async def test_delete_store_clears_items_and_removes_catalogue_row(env):
    """Delete drops the catalogue row + sets ``store=NULL`` on every
    item that referenced it. Returns the count of items cleared."""
    await env.repo.add("Milk", created_by="u1", store="Aldi")
    await env.repo.add("Eggs", created_by="u1", store="Aldi")
    await env.repo.add("Bread", created_by="u1", store="Bakery")

    cleared = await env.repo.delete_store("Aldi")

    assert cleared == 2
    stores = await env.repo.list_stores()
    assert "Aldi" not in [s.name for s in stores]
    items = await env.repo.list()
    by_text = {i.text: i for i in items}
    assert by_text["Milk"].store is None
    assert by_text["Eggs"].store is None
    assert by_text["Bread"].store == "Bakery"


async def test_delete_store_missing_is_zero(env):
    """Delete-on-missing returns zero rather than raising — operators
    double-clicking the trash icon shouldn't see an error."""
    await env.repo.touch_store("Aldi")
    cleared = await env.repo.delete_store("Migrso")
    assert cleared == 0
    stores = await env.repo.list_stores()
    assert [s.name for s in stores] == ["Aldi"]
