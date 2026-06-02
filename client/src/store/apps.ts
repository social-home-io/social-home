/**
 * Apps store — installed apps and catalog browsing.
 *
 * Mirrors ``GET /api/apps`` (installed) and ``GET /api/apps/catalog``
 * (admin-only browse surface) so the AppsPage and the dashboard card
 * can render without a per-render fetch round-trip.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { showToast } from '@/components/Toast'

export interface InstalledApp {
  app_id:       string
  name:         string
  version:      string
  enabled:      boolean
  capabilities: string[]
  icon:         string | null
}

export interface CatalogEntry {
  app_id:       string
  name:         string
  latest_version: string
  description:  string | null
  icon_url:     string | null
  capabilities: string[]
}

export const installedApps   = signal<InstalledApp[]>([])
export const catalog         = signal<CatalogEntry[]>([])
export const appsLoading     = signal(false)
export const catalogLoading  = signal(false)
export const appsError       = signal<string | null>(null)
export const catalogError    = signal<string | null>(null)

export async function loadInstalled(): Promise<void> {
  appsLoading.value = true
  appsError.value = null
  try {
    const data = await api.get('/api/apps') as { apps: InstalledApp[] }
    installedApps.value = data.apps ?? []
  } catch (err: unknown) {
    appsError.value = (err as Error).message ?? 'Could not load apps.'
  } finally {
    appsLoading.value = false
  }
}

export async function loadCatalog(): Promise<void> {
  catalogLoading.value = true
  catalogError.value = null
  try {
    const data = await api.get('/api/apps/catalog') as { apps: CatalogEntry[] }
    catalog.value = data.apps ?? []
  } catch (err: unknown) {
    catalogError.value = (err as Error).message ?? 'Could not load catalog.'
  } finally {
    catalogLoading.value = false
  }
}

export async function installApp(appId: string): Promise<void> {
  const app = await api.post('/api/apps', { app_id: appId }) as InstalledApp
  installedApps.value = [...installedApps.value, app]
  showToast(`${app.name} installed.`, 'success')
}

export async function uninstallApp(appId: string): Promise<void> {
  await api.delete(`/api/apps/${encodeURIComponent(appId)}`)
  installedApps.value = installedApps.value.filter(a => a.app_id !== appId)
}

export async function setEnabled(appId: string, enabled: boolean): Promise<void> {
  const updated = await api.patch(`/api/apps/${encodeURIComponent(appId)}`, { enabled }) as InstalledApp
  installedApps.value = installedApps.value.map(a => a.app_id === appId ? updated : a)
}

/** Test helper — reset signals without hitting the API. */
export function _resetAppsForTest(): void {
  installedApps.value   = []
  catalog.value         = []
  appsLoading.value     = false
  catalogLoading.value  = false
  appsError.value       = null
  catalogError.value    = null
}
