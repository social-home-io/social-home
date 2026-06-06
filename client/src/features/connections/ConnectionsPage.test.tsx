import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

// Mock the API module before importing the page
vi.mock('@/api', () => ({
  api: {
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))

// Mock auth store
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

// Mock i18n
vi.mock('@/i18n/i18n', () => ({
  t: (key: string) => key,
  locale: { value: 'en' },
  setLocale: vi.fn(),
}))

// Mock pageTitle
vi.mock('@/store/pageTitle', () => ({
  useTitle: () => {},
}))

// Mock PairingFlow and related components to avoid complex dependencies
vi.mock('@/components/PairingFlow', () => ({
  openPairing: vi.fn(),
  PairingFlow: () => null,
}))

vi.mock('@/components/ConfirmDialog', () => ({
  ConfirmDialog: () => null,
}))

vi.mock('@/components/AutoPairDialog', () => ({
  AutoPairDialog: () => null,
  openAutoPair: vi.fn(),
}))

vi.mock('@/components/ConnectionDetail', () => ({
  ConnectionDetail: () => null,
}))

vi.mock('@/components/Toast', () => ({
  showToast: vi.fn(),
}))

vi.mock('@/components/confirm', () => ({
  confirmDialog: vi.fn(),
}))

// WS mock with handler capture for transport_changed tests.
// Use vi.hoisted so the factory and the test variables share the same
// reference even though vi.mock is hoisted to the top of the file.
const { wsHandlers, wsMock } = vi.hoisted(() => {
  const wsHandlers = new Map<string, Set<(evt: { type: string; data: Record<string, unknown> }) => void>>()
  const wsMock = {
    on: vi.fn((type: string, handler: (evt: { type: string; data: Record<string, unknown> }) => void) => {
      if (!wsHandlers.has(type)) wsHandlers.set(type, new Set())
      wsHandlers.get(type)!.add(handler)
      return () => { wsHandlers.get(type)?.delete(handler) }
    }),
  }
  return { wsHandlers, wsMock }
})
vi.mock('@/ws', () => ({ ws: wsMock }))

import { api } from '@/api'
import ConnectionsPage from './ConnectionsPage'

const apiMock = api as unknown as { get: ReturnType<typeof vi.fn> }

function makeConnection(over: Record<string, unknown> = {}) {
  return {
    instance_id: 'inst-1',
    display_name: 'Household Alpha',
    status: 'confirmed',
    reachable: true,
    transport: null,
    ...over,
  }
}

describe('ConnectionsPage', () => {
  it('module exports a default component', async () => {
    const mod = await import('./ConnectionsPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  }, 20000)

  describe('transport glyph', () => {
    beforeEach(() => {
      wsHandlers.clear()
      wsMock.on.mockClear()
    })

    it('renders the RTC lightning glyph for transport=rtc', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/connections') return Promise.resolve([makeConnection({ transport: 'rtc' })])
        return Promise.resolve([])
      })

      const { container } = render(<ConnectionsPage />)

      await waitFor(() => {
        const icon = container.querySelector('.sh-transport-icon--rtc')
        expect(icon).not.toBeNull()
      })

      const icon = container.querySelector('.sh-transport-icon--rtc')!
      expect(icon.getAttribute('title')).toBe('Direct connection — low latency')
      expect(icon.getAttribute('aria-label')).toBe('Direct (WebRTC)')
    })

    it('renders the HTTPS cloud glyph for transport=https', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/connections') return Promise.resolve([makeConnection({ transport: 'https' })])
        return Promise.resolve([])
      })

      const { container } = render(<ConnectionsPage />)

      await waitFor(() => {
        const icon = container.querySelector('.sh-transport-icon--https')
        expect(icon).not.toBeNull()
      })

      const icon = container.querySelector('.sh-transport-icon--https')!
      expect(icon.getAttribute('title')).toBe('Via HTTPS — works, but slower than direct')
      expect(icon.getAttribute('aria-label')).toBe('Via HTTPS (fallback)')
    })

    it('renders no transport glyph when transport is null', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/connections') return Promise.resolve([makeConnection({ transport: null })])
        return Promise.resolve([])
      })

      const { container } = render(<ConnectionsPage />)

      await waitFor(() => {
        // Card should render
        expect(container.querySelector('.sh-connection-card')).not.toBeNull()
      })

      // Placeholder span reserves the 18px slot but renders nothing visible
      const slot = container.querySelector('.sh-transport-icon')
      expect(slot).not.toBeNull()
      expect(slot?.querySelector('svg')).toBeNull()
      expect(slot?.getAttribute('title')).toBeNull()
    })

    it('swaps the glyph in place when peer.transport_changed fires', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/connections') return Promise.resolve([makeConnection({ transport: 'https' })])
        return Promise.resolve([])
      })

      const { container } = render(<ConnectionsPage />)

      // Wait for HTTPS glyph to appear
      await waitFor(() => {
        expect(container.querySelector('.sh-transport-icon--https')).not.toBeNull()
      })

      // Fire the peer.transport_changed WS event
      const handlers = wsHandlers.get('peer.transport_changed')
      expect(handlers).toBeDefined()
      handlers!.forEach(h => h({
        type: 'peer.transport_changed',
        data: { type: 'peer.transport_changed', instance_id: 'inst-1', transport: 'rtc' },
      }))

      // HTTPS glyph gone, RTC glyph appears
      await waitFor(() => {
        expect(container.querySelector('.sh-transport-icon--https')).toBeNull()
        expect(container.querySelector('.sh-transport-icon--rtc')).not.toBeNull()
      })
    })
  })

  describe('List / Map view toggle', () => {
    beforeEach(() => {
      wsHandlers.clear()
      wsMock.on.mockClear()
      apiMock.get.mockResolvedValue([])
    })

    it('renders both List and Map tab buttons', async () => {
      const { getByRole } = render(<ConnectionsPage />)
      expect(getByRole('button', { name: 'List' })).toBeDefined()
      expect(getByRole('button', { name: 'Map' })).toBeDefined()
    })

    it('shows the lazy map fallback when Map tab is clicked', async () => {
      const { getByRole } = render(<ConnectionsPage />)

      // Click the Map tab — lazy Suspense fallback or map container appears
      getByRole('button', { name: 'Map' }).click()

      await waitFor(() => {
        // Either the Suspense fallback "Loading map…" or the rendered
        // FederationMap container is present.  In the test environment
        // the lazy module resolves synchronously so the testid wins.
        const container = document.querySelector('[data-testid="sh-federation-map"]')
        const fallback = document.querySelector('.sh-federation-map__loading')
        expect(container ?? fallback).not.toBeNull()
        // Guard: old placeholder text must NOT appear
        expect(document.body.textContent).not.toContain('Map coming in Task 11')
      })
    })

    it('List tab is aria-pressed=true by default', async () => {
      const { getByRole } = render(<ConnectionsPage />)
      const listBtn = getByRole('button', { name: 'List' })
      expect(listBtn.getAttribute('aria-pressed')).toBe('true')
      const mapBtn = getByRole('button', { name: 'Map' })
      expect(mapBtn.getAttribute('aria-pressed')).toBe('false')
    })

    it('Map tab becomes aria-pressed=true after click', async () => {
      const { getByRole } = render(<ConnectionsPage />)
      const mapBtn = getByRole('button', { name: 'Map' })
      mapBtn.click()
      await waitFor(() => {
        expect(mapBtn.getAttribute('aria-pressed')).toBe('true')
        expect(getByRole('button', { name: 'List' }).getAttribute('aria-pressed')).toBe('false')
      })
    })
  })

  describe('GFS connection status labels', () => {
    beforeEach(() => {
      wsHandlers.clear()
      wsMock.on.mockClear()
    })

    function makeGfs(over: Record<string, unknown> = {}) {
      return {
        id: 'gfs-1',
        gfs_instance_id: 'i1',
        display_name: 'Town GFS',
        inbox_url: 'https://gfs.example.com',
        status: 'active',
        paired_at: '2026-06-06T00:00:00+00:00',
        published_space_count: 0,
        ...over,
      }
    }

    it('renders the "Pending approval" label for a pending GFS', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/gfs/connections') return Promise.resolve([makeGfs({ status: 'pending' })])
        return Promise.resolve([])
      })

      const { findByText } = render(<ConnectionsPage />)
      expect(await findByText('gfs.status_pending')).toBeTruthy()
    })

    it('renders the "Suspended" label for a suspended GFS', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/gfs/connections') return Promise.resolve([makeGfs({ status: 'suspended' })])
        return Promise.resolve([])
      })

      const { findByText } = render(<ConnectionsPage />)
      expect(await findByText('gfs.status_suspended')).toBeTruthy()
    })

    it('renders no status label for an active GFS', async () => {
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/gfs/connections') return Promise.resolve([makeGfs({ status: 'active' })])
        return Promise.resolve([])
      })

      const { container, queryByText } = render(<ConnectionsPage />)
      await waitFor(() => {
        expect(container.querySelector('.sh-type-badge')).not.toBeNull()
      })
      expect(queryByText('gfs.status_pending')).toBeNull()
      expect(queryByText('gfs.status_suspended')).toBeNull()
    })
  })

})
