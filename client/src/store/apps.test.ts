import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  installedApps,
  catalog,
  loadInstalled,
  loadCatalog,
  installApp,
  uninstallApp,
  setEnabled,
  _resetAppsForTest,
} from './apps'

const fetchMock = vi.fn()

beforeEach(() => {
  _resetAppsForTest()
  fetchMock.mockReset()
  globalThis.fetch = fetchMock as unknown as typeof fetch
})

afterEach(() => {
  _resetAppsForTest()
})

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { 'content-type': 'application/json' },
  })
}

describe('apps store', () => {
  // ── loadInstalled ───────────────────────────────────────────────

  it('loadInstalled calls GET /api/apps and populates installedApps', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        apps: [
          { app_id: 'a1', name: 'Alpha', version: '1.0', enabled: true, capabilities: [], icon: null },
          { app_id: 'a2', name: 'Beta',  version: '2.0', enabled: false, capabilities: ['notify'], icon: null },
        ],
      }),
    )
    await loadInstalled()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toBe('api/apps')
    expect(installedApps.value).toHaveLength(2)
    expect(installedApps.value[0].app_id).toBe('a1')
  })

  it('loadInstalled sets appsError on failure', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ error: { code: 'SERVER_ERROR', detail: 'boom' } }, { status: 500 }),
    )
    const { appsError } = await import('./apps')
    await loadInstalled()
    expect(appsError.value).toBeTruthy()
    expect(installedApps.value).toHaveLength(0)
  })

  // ── loadCatalog ─────────────────────────────────────────────────

  it('loadCatalog calls GET /api/apps/catalog and populates catalog', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        apps: [
          { app_id: 'c1', name: 'CoolApp', latest_version: '3.0', description: 'desc', icon_url: null, capabilities: ['storage'] },
        ],
      }),
    )
    await loadCatalog()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toBe('api/apps/catalog')
    expect(catalog.value).toHaveLength(1)
    expect(catalog.value[0].app_id).toBe('c1')
  })

  // ── installApp ──────────────────────────────────────────────────

  it('installApp calls POST /api/apps with {app_id} and appends to installedApps', async () => {
    const newApp = { app_id: 'x1', name: 'Xapp', version: '0.1', enabled: true, capabilities: [], icon: null }
    fetchMock.mockResolvedValueOnce(jsonResponse(newApp, { status: 201 }))
    await installApp('x1')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, opts] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('api/apps')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body as string)).toEqual({ app_id: 'x1' })
    expect(installedApps.value).toHaveLength(1)
    expect(installedApps.value[0].app_id).toBe('x1')
  })

  // ── uninstallApp ────────────────────────────────────────────────

  it('uninstallApp calls DELETE /api/apps/{id} and removes from installedApps', async () => {
    installedApps.value = [
      { app_id: 'a1', name: 'Alpha', version: '1.0', enabled: true, capabilities: [], icon: null },
      { app_id: 'a2', name: 'Beta',  version: '2.0', enabled: true, capabilities: [], icon: null },
    ]
    fetchMock.mockResolvedValueOnce(new Response('{"status":"ok"}', { status: 200, headers: { 'content-type': 'application/json' } }))
    await uninstallApp('a1')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, opts] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('api/apps/a1')
    expect(opts.method).toBe('DELETE')
    expect(installedApps.value).toHaveLength(1)
    expect(installedApps.value[0].app_id).toBe('a2')
  })

  // ── setEnabled ──────────────────────────────────────────────────

  it('setEnabled calls PATCH /api/apps/{id} with {enabled} and updates signal', async () => {
    installedApps.value = [
      { app_id: 'a1', name: 'Alpha', version: '1.0', enabled: true, capabilities: [], icon: null },
    ]
    const updated = { app_id: 'a1', name: 'Alpha', version: '1.0', enabled: false, capabilities: [], icon: null }
    fetchMock.mockResolvedValueOnce(jsonResponse(updated))
    await setEnabled('a1', false)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, opts] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('api/apps/a1')
    expect(opts.method).toBe('PATCH')
    expect(JSON.parse(opts.body as string)).toEqual({ enabled: false })
    expect(installedApps.value[0].enabled).toBe(false)
  })
})
