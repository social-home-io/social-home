/**
 * Stickies store — household + space-scoped sticky notes (§19).
 *
 * Canonical row shape matches the backend (``content`` + ``position_x``
 * / ``position_y``), and the WS handlers now actually merge server
 * frames into the signal — prior to §SX1 the backend didn't publish
 * anything and this store was a placeholder.
 */
import { signal } from '@preact/signals'
import { ws } from '@/ws'

export interface StickyRow {
  id:         string
  author:     string
  content:    string
  color:      string
  position_x: number
  position_y: number
  created_at: string
  updated_at: string
  space_id:   string | null
}

/** All known stickies for the current scope. The page component sets
 * this from the REST list, WS handlers merge live updates in.   */
export const stickies = signal<StickyRow[]>([])

export function wireStickiesWs(): void {
  ws.on('sticky.created', (e) => {
    const s = e.data as unknown as StickyRow
    if (!stickies.value.some((x) => x.id === s.id)) {
      stickies.value = [...stickies.value, s]
    }
  })
  ws.on('sticky.updated', (e) => {
    const u = e.data as unknown as Partial<StickyRow> & { id: string }
    stickies.value = stickies.value.map((x) =>
      x.id === u.id ? { ...x, ...u } : x,
    )
  })
  ws.on('sticky.deleted', (e) => {
    const id = (e.data as { id: string }).id
    stickies.value = stickies.value.filter((x) => x.id !== id)
  })
}
