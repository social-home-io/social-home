import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'

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

import { pageTitle } from '@/store/pageTitle'
import { toggles } from '@/components/HouseholdToggles'

describe('FeedPage', () => {
  beforeEach(() => {
    pageTitle.value = ''
    toggles.value = null
  })

  it('module exports a default component', async () => {
    const mod = await import('./FeedPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  })

  it('falls back to "Home" while household toggles are loading', async () => {
    toggles.value = null
    const mod = await import('./FeedPage')
    const FeedPage = mod.default
    render(<FeedPage />)
    // useTitle runs in a useEffect; wait one tick for it to populate.
    await new Promise(r => setTimeout(r, 0))
    expect(pageTitle.value).toBe('Home')
  })

  it('uses household_name from the toggles store when available', async () => {
    toggles.value = {
      household_name: 'The Smiths',
      feat_feed: true, feat_pages: true, feat_tasks: true,
      feat_stickies: true, feat_calendar: true, feat_stories: true,
      feat_momentum: true,
      allow_text: true, allow_image: true, allow_video: true,
      allow_file: true, allow_poll: true, allow_schedule: true,
      allow_story_share: true,
    }
    const mod = await import('./FeedPage')
    const FeedPage = mod.default
    render(<FeedPage />)
    await new Promise(r => setTimeout(r, 0))
    expect(pageTitle.value).toBe('The Smiths')
  })
})
