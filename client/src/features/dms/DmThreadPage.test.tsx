import { describe, it, expect, vi, beforeEach } from 'vitest'

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

// ── Integration: mount DmThreadPage and assert the chip's render ─
// state matches the scroll-position story. jsdom doesn't lay out,
// so ``scrollHeight`` / ``clientHeight`` / ``scrollTop`` default to
// 0. We override them via ``Object.defineProperty`` to drive the
// two ends of the live-edge condition. Verifies the wiring between
// the layout effect, the post-paint follow-up, and the tail-
// tracking guard — not just the helper math.

const apiGet = vi.fn()
const apiPost = vi.fn()

vi.mock('preact-iso', () => ({
  // Pin the route so DmThreadPage's ``useRoute().params.id`` resolves
  // to a known conv-id; the real router would set this via
  // ``<Route path="/dms/:id">`` but we're mounting the page directly.
  useRoute: () => ({ params: { id: 'conv-test' }, path: '/dms/conv-test' }),
  useLocation: () => ({ url: '/dms/conv-test', route: vi.fn() }),
  lazy: (fn: () => Promise<{ default: unknown }>) => fn,
  LocationProvider: ({ children }: { children: unknown }) => children,
  Router: ({ children }: { children: unknown }) => children,
  Route: ({ component: C }: { component: () => unknown }) => C(),
  hydrate: vi.fn(),
  prerender: vi.fn(),
  ErrorBoundary: ({ children }: { children: unknown }) => children,
}))

vi.mock('@/api', async () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
    upload: vi.fn().mockResolvedValue({}),
  },
}))

vi.mock('@/ws', () => ({
  ws: { on: vi.fn(() => () => {}) },
}))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u-me', username: 'me', display_name: 'Me', is_admin: false, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 't' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

interface MockApiResponses {
  conversations: unknown[]
  messages: unknown[]
  members?: unknown[]
}

function wireApiMock(fixtures: MockApiResponses): void {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/conversations') return fixtures.conversations
    if (url.startsWith('/api/conversations/conv-test/messages')) {
      return fixtures.messages
    }
    if (url.startsWith('/api/conversations/conv-test/members')) {
      return fixtures.members ?? []
    }
    return []
  })
}

/** Force the messages scroll container's metrics so the live-edge
 *  math evaluates as if we were really laid out. jsdom returns 0
 *  for these by default, so without overriding the test would see
 *  ``distFromBottom = 0`` regardless of what we want to simulate. */
function stubScrollMetrics(opts: {
  scrollTop: number
  scrollHeight: number
  clientHeight: number
}): () => void {
  const proto = HTMLElement.prototype
  const orig = {
    scrollTop: Object.getOwnPropertyDescriptor(proto, 'scrollTop'),
    scrollHeight: Object.getOwnPropertyDescriptor(proto, 'scrollHeight'),
    clientHeight: Object.getOwnPropertyDescriptor(proto, 'clientHeight'),
  }
  Object.defineProperty(proto, 'scrollTop', {
    configurable: true, get: () => opts.scrollTop, set: () => {},
  })
  Object.defineProperty(proto, 'scrollHeight', {
    configurable: true, get: () => opts.scrollHeight,
  })
  Object.defineProperty(proto, 'clientHeight', {
    configurable: true, get: () => opts.clientHeight,
  })
  // ``scrollIntoView`` is a no-op in jsdom; this matches what
  // happens in production when the anchor message is already at the
  // visual bottom (the call does nothing because scrollTop is
  // already 0).
  if (!proto.scrollIntoView) {
    Object.defineProperty(proto, 'scrollIntoView', {
      configurable: true, value: () => {},
    })
  }
  return () => {
    if (orig.scrollTop) Object.defineProperty(proto, 'scrollTop', orig.scrollTop)
    if (orig.scrollHeight) Object.defineProperty(proto, 'scrollHeight', orig.scrollHeight)
    if (orig.clientHeight) Object.defineProperty(proto, 'clientHeight', orig.clientHeight)
  }
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  apiPost.mockReset()
  apiPost.mockResolvedValue({})
})

describe('DmThreadPage — jump-down chip integration', () => {
  it('does NOT render the chip when the entry-scroll lands at the visual bottom', async () => {
    const restore = stubScrollMetrics({
      scrollTop: 0, scrollHeight: 600, clientHeight: 600,
    })
    try {
      wireApiMock({
        conversations: [{
          id: 'conv-test',
          type: 'dm',
          name: null,
          last_message_at: '2026-05-17T13:00:42+00:00',
          members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null }],
          member_count: 2,
          unread: 1,
          last_read_at: '2026-05-17T13:00:29+00:00',
        }],
        messages: [{
          id: 'msg-new',
          sender_user_id: 'u-bob',
          content: 'BUG-REPRO: only one new message',
          type: 'text',
          media_url: null, file_name: null, mime_type: null,
          file_size_bytes: null, reply_to_id: null,
          reactions: [], deleted: false,
          created_at: '2026-05-17T13:00:42+00:00',
          edited_at: null,
        }],
        members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null, is_online: false, is_idle: false, last_seen_at: null }],
      })
      const { render, waitFor } = await import('@testing-library/preact')
      const { default: DmThreadPage } = await import('./DmThreadPage')
      const { container } = render(<DmThreadPage />)
      await waitFor(() => {
        expect(container.textContent ?? '').toContain('BUG-REPRO')
      }, { timeout: 3000 })
      // Give the layout effect + the follow-up useEffect a tick to settle.
      await new Promise(r => setTimeout(r, 50))
      const chip = container.querySelector('.sh-dm-jump-down')
      expect(chip).toBeNull()
    } finally {
      restore()
    }
  })

  it('DOES render the "New messages" divider when entering scrolled-up with unread', async () => {
    // Bigger scroll range + scrollTop well past the 80 px slack →
    // the entry-scroll's distFromBottom resolves to > 80, so the
    // anchor stays put and the "New messages" divider surfaces.
    // Confirms the fix didn't strip the divider in the legitimate
    // case (the chip itself only appears on subsequent WS arrivals
    // — entry-with-unread surfaces the divider, not the chip).
    const restore = stubScrollMetrics({
      scrollTop: -400, scrollHeight: 2000, clientHeight: 600,
    })
    try {
      wireApiMock({
        conversations: [{
          id: 'conv-test',
          type: 'dm',
          name: null,
          last_message_at: '2026-05-17T13:00:42+00:00',
          members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null }],
          member_count: 2,
          unread: 5,
          last_read_at: '2026-05-17T12:00:00+00:00',
        }],
        // Backend returns ``ORDER BY created_at DESC`` (newest first);
        // the SPA reverses to render oldest→newest. Fixture mirrors
        // the DESC shape: index 0 = newest, index 29 = oldest. Newest
        // 5 are unread (after last_read_at).
        messages: Array.from({ length: 30 }).map((_, i) => ({
          id: `msg-${29 - i}`,
          sender_user_id: 'u-bob',
          content: `msg ${29 - i}`,
          type: 'text',
          media_url: null, file_name: null, mime_type: null,
          file_size_bytes: null, reply_to_id: null,
          reactions: [], deleted: false,
          created_at: i < 5
            ? '2026-05-17T13:00:42+00:00'
            : '2026-05-17T11:00:00+00:00',
          edited_at: null,
        })),
        members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null, is_online: false, is_idle: false, last_seen_at: null }],
      })
      const { render, waitFor } = await import('@testing-library/preact')
      const { default: DmThreadPage } = await import('./DmThreadPage')
      const { container } = render(<DmThreadPage />)
      await waitFor(() => {
        expect(container.querySelectorAll('[data-msg-id]').length).toBeGreaterThan(0)
      }, { timeout: 3000 })
      await new Promise(r => setTimeout(r, 50))
      // distFromBottom = 400 > 80 so the entry-scroll layout effect
      // leaves stickToBottom=false. The follow-up effect must NOT
      // fire the read-mark POST — the user hasn't actually caught
      // up. The chip itself stays hidden because the tail-tracking
      // guard skips the initial population.
      const readPosts = apiPost.mock.calls.filter(
        ([url]) => typeof url === 'string'
          && url.startsWith('/api/conversations/conv-test/read'),
      )
      expect(readPosts).toHaveLength(0)
      const chip = container.querySelector('.sh-dm-jump-down')
      expect(chip).toBeNull()
    } finally {
      restore()
    }
  })

  it('auto-stamps the read watermark when entry-scroll lands at the live edge', async () => {
    // Positive-shape companion to the test above: when
    // ``isAtLiveEdge`` resolves to true, the follow-up useEffect
    // fires the read-mark POST. This is the contract that keeps
    // a subsequent inbound WS message from surfacing a chip the
    // user has already "seen" in the same entry.
    const restore = stubScrollMetrics({
      scrollTop: 0, scrollHeight: 600, clientHeight: 600,
    })
    try {
      wireApiMock({
        conversations: [{
          id: 'conv-test',
          type: 'dm',
          name: null,
          last_message_at: '2026-05-17T13:00:42+00:00',
          members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null }],
          member_count: 2,
          unread: 1,
          last_read_at: '2026-05-17T13:00:29+00:00',
        }],
        messages: [{
          id: 'msg-new',
          sender_user_id: 'u-bob',
          content: 'only one new message',
          type: 'text',
          media_url: null, file_name: null, mime_type: null,
          file_size_bytes: null, reply_to_id: null,
          reactions: [], deleted: false,
          created_at: '2026-05-17T13:00:42+00:00',
          edited_at: null,
        }],
        members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null, is_online: false, is_idle: false, last_seen_at: null }],
      })
      const { render, waitFor } = await import('@testing-library/preact')
      const { default: DmThreadPage } = await import('./DmThreadPage')
      const { container } = render(<DmThreadPage />)
      await waitFor(() => {
        expect(container.querySelectorAll('[data-msg-id]').length).toBeGreaterThan(0)
      }, { timeout: 3000 })
      await new Promise(r => setTimeout(r, 50))
      const readPosts = apiPost.mock.calls.filter(
        ([url]) => typeof url === 'string'
          && url.startsWith('/api/conversations/conv-test/read'),
      )
      expect(readPosts.length).toBeGreaterThanOrEqual(1)
    } finally {
      restore()
    }
  })
})
