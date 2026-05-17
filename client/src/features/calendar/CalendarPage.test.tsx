import { describe, it, expect, vi } from 'vitest'

// Mock the API module before importing the page. Per-test mocks
// override the default no-op shape via ``vi.mocked(api.get).mockImplementation``.
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

// Title hook + household users + WS are out-of-scope for these tests.
vi.mock('@/store/pageTitle', () => ({ useTitle: vi.fn() }))
vi.mock('@/store/householdUsers', () => ({
  householdUsers: { value: new Map() },
  loadHouseholdUsers: vi.fn().mockResolvedValue(undefined),
}))

describe('CalendarPage', () => {
  it('module exports a default component', async () => {
    const mod = await import('./CalendarPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  })

  it('renders day-group headings in chronological order regardless of creation order', async () => {
    // Regression for the bug where three events scheduled for
    // 2026-05-14, 2026-05-19 and 2026-05-21 surfaced as 14 → 21 →
    // 19 in the agenda. The root cause was a locale-fragile
    // ``new Date(toLocaleDateString())`` round-trip in the day-key
    // sort; this test pins the rendered order at the SPA boundary.
    const { api } = await import('@/api')
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      if (url === '/api/calendars') {
        return [{
          id: 'cal-1',
          name: 'Family',
          owner_username: 'admin',
          color: null,
        }]
      }
      if (url.startsWith('/api/calendars/cal-1/events')) {
        // Order intentionally NOT chronological to mimic the
        // multi-calendar ``responses.flat()`` shape in the bug
        // report. The page must surface them in event-date order
        // anyway.
        return [
          {
            id: 'e14', calendar_id: 'cal-1', summary: 'On the 14th',
            description: null,
            start: '2026-05-14T10:00:00Z', end: '2026-05-14T11:00:00Z',
            all_day: false, rrule: null, capacity: null,
            created_by: 'u1', attendees: ['u1'],
            rsvp_enabled: false, location: null, cover_url: null,
          },
          {
            id: 'e21', calendar_id: 'cal-1', summary: 'On the 21st',
            description: null,
            start: '2026-05-21T10:00:00Z', end: '2026-05-21T11:00:00Z',
            all_day: false, rrule: null, capacity: null,
            created_by: 'u1', attendees: ['u1'],
            rsvp_enabled: false, location: null, cover_url: null,
          },
          {
            id: 'e19', calendar_id: 'cal-1', summary: 'On the 19th',
            description: null,
            start: '2026-05-19T10:00:00Z', end: '2026-05-19T11:00:00Z',
            all_day: false, rrule: null, capacity: null,
            created_by: 'u1', attendees: ['u1'],
            rsvp_enabled: false, location: null, cover_url: null,
          },
        ]
      }
      return []
    })

    const { render, waitFor } = await import('@testing-library/preact')
    const mod = await import('./CalendarPage')
    const { container } = render(<mod.default />)

    // Wait for the async load to settle and all three day headings
    // to be rendered.
    await waitFor(() => {
      const titles = container.querySelectorAll('.sh-event strong')
      expect(titles.length).toBe(3)
    }, { timeout: 2000 })

    const eventTitles = Array.from(
      container.querySelectorAll('.sh-event strong'),
    ).map(el => el.textContent)
    expect(eventTitles).toEqual([
      'On the 14th', 'On the 19th', 'On the 21st',
    ])
  })
})
