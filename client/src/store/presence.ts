/**
 * Presence store — household member presence driven by
 * `presence.updated` WS frames (§22).
 */
import { signal } from '@preact/signals'
import { ws } from '@/ws'

export interface PresenceEntry {
  username:   string
  state:      string
  zone_name?: string | null
  latitude?:  number | null
  longitude?: number | null
}

export const presence = signal<Record<string, PresenceEntry>>({})

export function wirePresenceWs(): void {
  ws.on('presence.updated', (e) => {
    const data = e.data as unknown as PresenceEntry
    if (!data?.username) return
    presence.value = { ...presence.value, [data.username]: data }
  })
}
