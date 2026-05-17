import { describe, it, expect, vi } from 'vitest'

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

describe('DmThreadPage', () => {
  it('module exports a default component', async () => {
    const mod = await import('./DmThreadPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  })
})

describe('isAtLiveEdge', () => {
  // The "live edge" threshold (80 px) is the shared input to two
  // call sites: the user-scroll handler (``handleScroll``) and the
  // notification-driven entry effect (the anchor-scroll
  // ``useLayoutEffect``). Both must agree, or the jump-down chip
  // shows when the user is visually at the bottom — which is the
  // exact bug the helper unifies.

  it('treats a column-reverse container at scrollTop=0 as the live edge', async () => {
    // Chrome / Safari / Edge / modern Firefox land at scrollTop=0
    // when the latest message is in view in a column-reverse list.
    const { isAtLiveEdge } = await import('./DmThreadPage')
    expect(isAtLiveEdge({
      scrollTop: 0,
      scrollHeight: 800,
      clientHeight: 600,
    })).toBe(true)
  })

  it('returns true when within 80 px of the bottom (slack window)', async () => {
    const { isAtLiveEdge } = await import('./DmThreadPage')
    expect(isAtLiveEdge({
      scrollTop: -79,
      scrollHeight: 800,
      clientHeight: 600,
    })).toBe(true)
  })

  it('returns false past the 80 px slack window', async () => {
    const { isAtLiveEdge } = await import('./DmThreadPage')
    expect(isAtLiveEdge({
      scrollTop: -200,
      scrollHeight: 800,
      clientHeight: 600,
    })).toBe(false)
  })

  it('also handles the legacy positive-scrollTop convention', async () => {
    // ``scrollTop = maxScroll`` is the visual bottom on older
    // Firefox's positive-scrollTop column-reverse.
    const { isAtLiveEdge } = await import('./DmThreadPage')
    expect(isAtLiveEdge({
      scrollTop: 200,
      scrollHeight: 800,
      clientHeight: 600,
    })).toBe(true)
  })

  it('returns true when the content fits in the viewport (no scrollable range)', async () => {
    // Regression for the reported notification → DM flow: a single
    // unread message at the bottom can mean ``scrollHeight ==
    // clientHeight`` (or close enough), so ``distFromBottom = 0``
    // and the user is at the live edge — the chip must NOT render.
    const { isAtLiveEdge } = await import('./DmThreadPage')
    expect(isAtLiveEdge({
      scrollTop: 0,
      scrollHeight: 600,
      clientHeight: 600,
    })).toBe(true)
  })
})
