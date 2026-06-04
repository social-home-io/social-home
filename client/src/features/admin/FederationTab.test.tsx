import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

// The Federation tab loads via the federationCompat store
// (GET /api/admin/federation/compat). We drive it through the api mock so the
// real store code populates the signals — AdminPage's loadAll() preload and
// the tab's own useEffect both hit this mock, so seeding signals directly
// would be clobbered.
const apiGet = vi.fn()
let compatPayload: { ours: number; peers: unknown[] } = { ours: 0, peers: [] }
apiGet.mockImplementation((path: string) => {
  if (path === '/api/admin/federation/compat') return Promise.resolve(compatPayload)
  return Promise.resolve([])
})
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))
vi.mock('@/platform', () => ({
  usesHaUserDirectory: () => false,
  managesLocalUsers: () => true,
}))
vi.mock('@/components/Spinner', () => ({ Spinner: () => null }))

import AdminPage from './AdminPage'
import { _resetFederationCompatForTest } from '@/store/federationCompat'

const tick = () => new Promise((r) => setTimeout(r, 0))

beforeEach(() => {
  _resetFederationCompatForTest()
  compatPayload = { ours: 0, peers: [] }
})

async function renderFederationTab() {
  // AdminPage starts on 'members'; loadAll() flips loading false. Render,
  // settle, then click the Federation tab button to mount FederationTab.
  const utils = render(<AdminPage />)
  await tick(); await tick()
  const fedBtn = utils.getByRole('tab', { name: /Federation/ })
  fireEvent.click(fedBtn)
  await tick()
  return utils
}

describe('FederationTab', () => {
  it('renders all three status states', async () => {
    compatPayload = {
      ours: 18,
      peers: [
        {
          instance_id: 'i1', display_name: 'Alpha', proto_version: 18,
          status: 'confirmed', last_reachable_at: null,
          capabilities_known: true, lacking_features: [],
        },
        {
          instance_id: 'i2', display_name: 'Beta', proto_version: 15,
          status: 'confirmed', last_reachable_at: null,
          capabilities_known: true, lacking_features: ['Bazaar bids'],
        },
        {
          instance_id: 'i3', display_name: 'Gamma', proto_version: 1,
          status: 'confirmed', last_reachable_at: null,
          capabilities_known: false, lacking_features: [],
        },
      ],
    }

    const { container } = await renderFederationTab()
    const text = container.textContent || ''

    expect(text).toContain('Up to date ✓')
    expect(text).toContain('1 behind')
    expect(text).toContain('Bazaar bids')
    expect(text).toContain('Version unknown')
    // ours surfaced in the header
    expect(text).toContain('v18')
  })

  it('shows the "N peers behind" badge on the tab label', async () => {
    compatPayload = {
      ours: 18,
      peers: [
        {
          instance_id: 'i2', display_name: 'Beta', proto_version: 15,
          status: 'confirmed', last_reachable_at: null,
          capabilities_known: true, lacking_features: ['Bazaar bids'],
        },
        // unknown caps → must NOT inflate the badge count
        {
          instance_id: 'i3', display_name: 'Gamma', proto_version: 1,
          status: 'confirmed', last_reachable_at: null,
          capabilities_known: false, lacking_features: [],
        },
      ],
    }

    const { container } = render(<AdminPage />)
    await tick(); await tick()
    const badge = container.querySelector('.sh-tab-badge')
    expect(badge).not.toBeNull()
    expect(badge!.textContent).toBe('1')
  })
})
