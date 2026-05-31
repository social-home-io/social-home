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

/** Scope of the sticky board currently mounted: a ``space_id`` for a
 *  space board, or ``null`` for the household board. The ``stickies``
 *  signal feeds BOTH boards (``StickyBoardPage``), so WS handlers below
 *  short-circuit when an inbound frame's scope doesn't match — without
 *  this gate, a sticky created on a space board leaks into the
 *  household board (and vice versa). The page owns this signal: set on
 *  load, reset to ``null`` on unmount. Mirrors calendar.ts's
 *  ``activeCalendarScope``. */
export const activeStickyScope = signal<string | null>(null)

/** True when an inbound sticky frame's scope (``space_id``, ``null`` =
 *  household) matches the currently-mounted board. */
function _scopedToActive(spaceId: string | null | undefined): boolean {
  return (spaceId ?? null) === activeStickyScope.value
}

export function wireStickiesWs(): void {
  ws.on('sticky.created', (e) => {
    const s = e.data as unknown as StickyRow
    if (!_scopedToActive(s.space_id)) return
    if (!stickies.value.some((x) => x.id === s.id)) {
      stickies.value = [...stickies.value, s]
    }
  })
  ws.on('sticky.updated', (e) => {
    const u = e.data as unknown as Partial<StickyRow> & { id: string }
    if (!_scopedToActive(u.space_id)) return
    stickies.value = stickies.value.map((x) =>
      x.id === u.id ? { ...x, ...u } : x,
    )
  })
  ws.on('sticky.deleted', (e) => {
    const { id, space_id } = e.data as { id: string; space_id?: string | null }
    if (!_scopedToActive(space_id)) return
    stickies.value = stickies.value.filter((x) => x.id !== id)
  })
}
