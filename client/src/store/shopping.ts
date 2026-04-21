import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import type { ShoppingItem } from '@/types'

export const items = signal<ShoppingItem[]>([])

export async function loadShopping() {
  // Include completed so the "Re-add recent" suggestion chips have
  // something to show. The component sorts by `completed` for render.
  items.value = await api.get('/api/shopping?include_completed=true')
}

export async function addItem(text: string) {
  // Optimistic by way of WS fan-out: the POST response also comes back
  // via shopping_list.item_added for every other device in the
  // household. The caller need not append locally — the WS listener
  // upserts the item by id (idempotent).
  const item = await api.post('/api/shopping', { text })
  _upsert(item as ShoppingItem)
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
    _upsert(e.data as unknown as ShoppingItem)
  })
  ws.on('shopping_list.item_updated', (e) => {
    const patch = e.data as unknown as Partial<ShoppingItem> & { id: string }
    items.value = items.value.map((i) =>
      i.id === patch.id ? { ...i, ...patch } : i,
    )
  })
  ws.on('shopping_list.item_removed', (e) => {
    const id = (e.data as { id: string }).id
    items.value = items.value.filter((i) => i.id !== id)
  })
  ws.on('shopping_list.cleared', () => {
    items.value = items.value.filter((i) => !i.completed)
  })
}
