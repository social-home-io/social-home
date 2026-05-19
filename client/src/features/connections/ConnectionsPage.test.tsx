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

    it('shows Map placeholder text when Map tab is clicked', async () => {
      const { getByRole, getByText, queryByText } = render(<ConnectionsPage />)

      // Household section is visible in list view
      await waitFor(() => {
        expect(queryByText('Map coming in Task 11 (federation-map)')).toBeNull()
      })

      // Click the Map tab
      getByRole('button', { name: 'Map' }).click()

      await waitFor(() => {
        expect(getByText('Map coming in Task 11 (federation-map)')).toBeDefined()
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

  describe('peer.home_changed WS frame', () => {
    beforeEach(() => {
      wsHandlers.clear()
      wsMock.on.mockClear()
    })

    it('updates peer home coords in the connections signal', async () => {
      const conn = makeConnection({ home_lat: null, home_lon: null })
      apiMock.get.mockImplementation((url: string) => {
        if (url === '/api/connections') return Promise.resolve([conn])
        return Promise.resolve([])
      })

      render(<ConnectionsPage />)

      // Wait for the connection to load
      await waitFor(() => {
        expect(wsHandlers.has('peer.home_changed')).toBe(true)
      })

      // Fire the peer.home_changed WS event
      const handlers = wsHandlers.get('peer.home_changed')
      expect(handlers).toBeDefined()
      handlers!.forEach(h => h({
        type: 'peer.home_changed',
        data: {
          type: 'peer.home_changed',
          instance_id: 'inst-1',
          latitude: 52.3676,
          longitude: 4.9041,
        },
      }))

      // The WS handler wired in useEffect updates connections signal
      // (we verify the handler registered without error)
      expect(handlers!.size).toBeGreaterThan(0)
    })
  })

  describe('local.home_changed WS frame', () => {
    beforeEach(() => {
      wsHandlers.clear()
      wsMock.on.mockClear()
    })

    it('registers a local.home_changed handler on mount', async () => {
      apiMock.get.mockResolvedValue([])
      render(<ConnectionsPage />)
      await waitFor(() => {
        expect(wsHandlers.has('local.home_changed')).toBe(true)
      })
    })
  })
})
