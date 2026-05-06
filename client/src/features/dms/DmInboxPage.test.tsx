import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'
import { LocationProvider } from 'preact-iso'

const apiGet = vi.fn()

vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))

vi.mock('@/ws', () => ({
  ws: { on: vi.fn(() => () => {}) },
}))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

vi.mock('@/store/calls', () => ({
  active: { value: [] },
}))

const FIXTURES = [
  { id: 'c-dm-1',  type: 'dm',       members: [{ user_id: 'p1', display_name: 'Anna',  picture_url: null }], last_message_at: null, name: null, member_count: 2 },
  { id: 'c-grp-1', type: 'group_dm', members: [{ user_id: 'p2', display_name: 'Bob',   picture_url: null }, { user_id: 'p3', display_name: 'Carol', picture_url: null }], last_message_at: null, name: 'Trip', member_count: 3 },
]

function renderAt(path: string) {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/conversations') return FIXTURES
    if (url === '/api/calls/active')  return []
    return []
  })
  window.history.pushState(null, '', path)
  // Re-import to reset module-level signals across renderAt() calls.
  return import('./DmInboxPage').then(({ default: DmInboxPage }) =>
    render(
      <LocationProvider>
        <DmInboxPage />
      </LocationProvider>,
    ),
  )
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
})

describe('DmInboxPage', () => {
  it('module exports a default component', async () => {
    const mod = await import('./DmInboxPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  })

  it('renders all three tab buttons', async () => {
    const { findByText } = await renderAt('/dms')
    expect(await findByText('DMs')).toBeTruthy()
    expect(await findByText('Groups')).toBeTruthy()
    expect(await findByText('Calls')).toBeTruthy()
  })

  it('defaults to the DMs tab and shows 1:1 conversations only', async () => {
    const { findByText, queryByText } = await renderAt('/dms')
    expect(await findByText('Anna')).toBeTruthy()
    // Group conversation is filtered out on the DMs tab.
    expect(queryByText('Trip')).toBeNull()
  })

  it('switches to Groups when its tab is clicked', async () => {
    const { findByText, getByText, queryByText } = await renderAt('/dms')
    await findByText('Anna')
    fireEvent.click(getByText('Groups'))
    await waitFor(() => expect(queryByText('Trip')).toBeTruthy())
    expect(queryByText('Anna')).toBeNull()
  })

  it('selects the Calls tab when ?tab=calls is set', async () => {
    const { findByText } = await renderAt('/dms?tab=calls')
    // Calls tab renders the empty-state header from CallsTab.
    expect(await findByText('No active calls')).toBeTruthy()
  })
})
