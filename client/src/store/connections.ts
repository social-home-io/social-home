/**
 * Connections store — paired federation instances and their
 * reachability, driven by `connection.reachable` and
 * `connection.unreachable` WS frames (§23.71).
 *
 * NetworkMap + ConnectionsPage both read :data:`connections`.
 */
import { signal } from '@preact/signals'
import { ws } from '@/ws'

export interface Connection {
  instance_id:   string
  display_name?: string
  pairing_status?: string
  reachable:     boolean
  last_seen_at?: string | null
}

export const connections = signal<Connection[]>([])

function upsert(patch: Partial<Connection> & { instance_id: string }): void {
  const existing = connections.value.find((c) => c.instance_id === patch.instance_id)
  if (existing) {
    connections.value = connections.value.map((c) =>
      c.instance_id === patch.instance_id ? { ...c, ...patch } : c,
    )
  } else {
    connections.value = [
      ...connections.value,
      { reachable: true, ...patch } as Connection,
    ]
  }
}

export function wireConnectionsWs(): void {
  ws.on('connection.reachable', (e) => {
    const d = e.data as unknown as { instance_id: string, last_seen_at?: string }
    if (!d?.instance_id) return
    upsert({
      instance_id:  d.instance_id,
      reachable:    true,
      last_seen_at: d.last_seen_at ?? null,
    })
  })
  ws.on('connection.unreachable', (e) => {
    const d = e.data as unknown as { instance_id: string }
    if (!d?.instance_id) return
    upsert({ instance_id: d.instance_id, reachable: false })
  })
  ws.on('connection.added', (e) => {
    const d = e.data as unknown as Connection
    if (!d?.instance_id) return
    upsert(d)
  })
  ws.on('connection.removed', (e) => {
    const d = e.data as unknown as { instance_id: string }
    if (!d?.instance_id) return
    connections.value = connections.value.filter((c) => c.instance_id !== d.instance_id)
  })
}
