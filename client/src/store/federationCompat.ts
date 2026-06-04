/**
 * Federation-compatibility store — protocol-version skew across paired peers.
 *
 * Mirrors ``GET /api/admin/federation/compat`` (admin-only): our own
 * ``proto_version`` plus a row per confirmed peer with its version, last
 * reachability, and the list of features it's missing relative to us. Drives
 * the Admin → Federation panel and the tab-label "N peers behind" badge so an
 * admin sees skew without opening the tab.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'

export interface CompatPeer {
  instance_id:       string
  display_name:      string
  proto_version:     number
  status:            string
  last_reachable_at: string | null
  capabilities_known: boolean
  lacking_features:  string[]
}

export const compatPeers   = signal<CompatPeer[]>([])
export const compatOurs    = signal(0)
export const compatLoading  = signal(false)
export const compatError    = signal<string | null>(null)

export async function loadFederationCompat(): Promise<void> {
  compatLoading.value = true
  compatError.value = null
  try {
    const data = await api.get('/api/admin/federation/compat') as {
      ours: number
      peers: CompatPeer[]
    }
    compatOurs.value = data.ours ?? 0
    compatPeers.value = data.peers ?? []
  } catch (err: unknown) {
    compatError.value = (err as Error).message ?? 'Could not load federation compatibility.'
    compatPeers.value = []
  } finally {
    compatLoading.value = false
  }
}

/**
 * Count peers genuinely behind our protocol version. A peer whose
 * capabilities we haven't learned yet (``capabilities_known === false``) is
 * NOT counted — its reported ``proto_version`` is a placeholder until the
 * first ``INSTANCE_CAPABILITIES_UPDATED`` handshake, so counting it would
 * raise a phantom "peer behind" nag on every first contact.
 */
export function peersBehindCount(): number {
  return compatPeers.value.filter(
    (p) => p.capabilities_known && p.proto_version < compatOurs.value,
  ).length
}

/** Test helper — reset signals without hitting the API. */
export function _resetFederationCompatForTest(): void {
  compatPeers.value   = []
  compatOurs.value    = 0
  compatLoading.value = false
  compatError.value   = null
}
