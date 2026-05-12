import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import type { ShoppingItem, ShoppingStore } from '@/types'

export const items = signal<ShoppingItem[]>([])
export const stores = signal<ShoppingStore[]>([])

export async function loadShopping() {
  // Include completed so the "Re-add recent" suggestion chips have
  // something to show. The component sorts by `completed` for render.
  // Stores are fetched in parallel — the grouped view needs them
  // before the first paint to render section headers in trip order.
  const [itemsResp, storesResp] = await Promise.all([
    api.get('/api/shopping?include_completed=true'),
    api.get('/api/shopping/stores'),
  ])
  items.value = itemsResp as ShoppingItem[]
  stores.value = storesResp as ShoppingStore[]
}

export async function addItem(text: string, store: string | null = null) {
  // Optimistic by way of WS fan-out: the POST response also comes back
  // via shopping_list.item_added for every other device in the
  // household. The caller need not append locally — the WS listener
  // upserts the item by id (idempotent).
  const body: Record<string, unknown> = { text }
  if (store) body.store = store
  const item = await api.post('/api/shopping', body)
  _upsert(item as ShoppingItem)
  // The server auto-upserts a catalogue row on first sighting; pull
  // the fresh list so the new section appears immediately on the page
  // that added it (the WS fan-out doesn't carry a "new store" frame —
  // adding a new store is unambiguous from ``item.store``).
  if (store && !stores.value.some(s => s.name === store)) {
    await reloadStores()
  }
}

export async function updateItem(
  id: string,
  patch: { text?: string; store?: string | null },
) {
  const prev = items.value
  // Optimistic patch — reconcile from WS event / error.
  items.value = items.value.map((i) =>
    i.id === id ? { ...i, ...patch } : i,
  )
  try {
    const fresh = await api.patch(`/api/shopping/${id}`, patch)
    _upsert(fresh as ShoppingItem)
    if (patch.store && !stores.value.some(s => s.name === patch.store)) {
      await reloadStores()
    }
  } catch (err) {
    items.value = prev
    throw err
  }
}

export async function toggleItem(id: string, nextCompleted: boolean) {
  const prev = items.value
  // Optimistic local update — reconcile from WS event / error.
  items.value = items.value.map((i) =>
    i.id === id ? { ...i, completed: nextCompleted } : i,
  )
  try {
    await api.patch(
      `/api/shopping/${id}/${nextCompleted ? 'complete' : 'uncomplete'}`,
    )
  } catch (err) {
    items.value = prev
    throw err
  }
}

export async function deleteItem(id: string) {
  const prev = items.value
  items.value = items.value.filter((i) => i.id !== id)
  try {
    await api.delete(`/api/shopping/${id}`)
  } catch (err) {
    items.value = prev
    throw err
  }
}

export async function clearCompleted() {
  const prev = items.value
  items.value = items.value.filter((i) => !i.completed)
  try {
    await api.post('/api/shopping/clear-completed', {})
  } catch (err) {
    items.value = prev
    throw err
  }
}

export async function reorderStores(orderedNames: string[]) {
  const prev = stores.value
  // Optimistic: shuffle the local store list so the section headers
  // animate to their new positions immediately. The server returns
  // the canonical post-reorder list which we then replace verbatim.
  const byName = new Map(prev.map(s => [s.name, s]))
  const optimistic: ShoppingStore[] = []
  orderedNames.forEach((name, i) => {
    const existing = byName.get(name)
    if (existing) optimistic.push({ name, sort_order: i })
  })
  // Carry over any rows the caller forgot — they shift past the end,
  // mirroring the server-side rule.
  prev.forEach((s) => {
    if (!orderedNames.includes(s.name)) {
      optimistic.push({ name: s.name, sort_order: optimistic.length })
    }
  })
  stores.value = optimistic
  try {
    const fresh = (await api.put('/api/shopping/stores/order', {
      order: orderedNames,
    })) as ShoppingStore[]
    stores.value = fresh
  } catch (err) {
    stores.value = prev
    throw err
  }
}

async function reloadStores() {
  stores.value = (await api.get('/api/shopping/stores')) as ShoppingStore[]
}

function _upsert(item: ShoppingItem) {
  const existing = items.value.findIndex((i) => i.id === item.id)
  if (existing >= 0) {
    items.value = items.value.map((i) =>
      i.id === item.id ? { ...i, ...item } : i,
    )
  } else {
    items.value = [...items.value, item]
  }
}

// ─── WS event handlers (§23.120.3, local household only) ────────────────

let _wired = false

/** Wire the shopping_list.* events into the local store so other
 *  clients' changes appear without a manual refresh. Idempotent. */
export function wireShoppingWs() {
  if (_wired) return
  _wired = true
  ws.on('shopping_list.item_added', (e) => {
    const item = e.data as unknown as ShoppingItem
    _upsert(item)
    // A new store name from a sibling tab → refresh the catalogue
    // (the server-side ``touch_store`` already wrote the row; we
    // just don't have it yet on the read side).
    if (item.store && !stores.value.some(s => s.name === item.store)) {
      void reloadStores()
    }
  })
  ws.on('shopping_list.item_updated', (e) => {
    const patch = e.data as unknown as Partial<ShoppingItem> & { id: string }
    items.value = items.value.map((i) =>
      i.id === patch.id ? { ...i, ...patch } : i,
    )
    if (patch.store && !stores.value.some(s => s.name === patch.store)) {
      void reloadStores()
    }
  })
  ws.on('shopping_list.item_removed', (e) => {
    const id = (e.data as { id: string }).id
    items.value = items.value.filter((i) => i.id !== id)
  })
  ws.on('shopping_list.cleared', () => {
    items.value = items.value.filter((i) => !i.completed)
  })
  ws.on('shopping_list.stores_reordered', (e) => {
    const order = (e.data as { order: string[] }).order
    // ``order`` is the canonical post-reorder name sequence — the
    // server only carries the new index, not any per-store metadata,
    // so we just rebuild the local catalogue from it.
    stores.value = order.map((name, i) => ({ name, sort_order: i }))
  })
}
