import { signal } from '@preact/signals'
import { api } from '@/api'

/**
 * Instance metadata fetched from `GET /api/instance/config`.
 *
 * Public endpoint (no token required). The SPA queries this on cold
 * start to decide whether to redirect to `/setup` and which login flow
 * to render. Re-fetched only on cold start; once the SPA has any
 * evidence the instance is set up (a 200 from /api/me) we don't
 * re-poll within the session.
 */
export interface InstanceConfig {
  mode: 'standalone' | 'ha' | 'haos'
  instance_name: string
  capabilities: string[]
  setup_required: boolean
}

export const instanceConfig = signal<InstanceConfig | null>(null)
export const instanceConfigError = signal<string | null>(null)

let inflight: Promise<InstanceConfig> | null = null

export async function loadInstanceConfig(): Promise<InstanceConfig> {
  if (inflight) return inflight
  inflight = api.get('/api/instance/config')
    .then((cfg) => {
      const typed = cfg as InstanceConfig
      instanceConfig.value = typed
      instanceConfigError.value = null
      return typed
    })
    .catch((err) => {
      instanceConfigError.value = err?.message || 'Failed to load instance config.'
      throw err
    })
    .finally(() => { inflight = null })
  return inflight
}

export function hasCapability(cap: string): boolean {
  return instanceConfig.value?.capabilities.includes(cap) ?? false
}
