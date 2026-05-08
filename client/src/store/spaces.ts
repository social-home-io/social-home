import { signal } from '@preact/signals'
import { api } from '@/api'
import type { Space } from '@/types'

export const spaces       = signal<Space[]>([])
export const activeSpace  = signal<Space | null>(null)

/** Refresh the cached spaces list from the server. Used by the spaces
 *  list page on mount and by SpaceCreateDialog after a successful
 *  create so the new row shows up without a manual reload. */
export async function loadSpaces(): Promise<void> {
  try {
    const rows = await api.get('/api/spaces') as Space[] | undefined | null
    // Defend against test mocks of ``@/api`` that don't configure a
    // resolved value — ``api.get(...)`` returns undefined, ``await
    // undefined`` resolves to undefined, and assigning that to the
    // signal would break consumers that call ``spaces.value.length``.
    spaces.value = Array.isArray(rows) ? rows : []
  } catch {
    /* leave the cached list — the page renders an empty / stale state. */
  }
}
