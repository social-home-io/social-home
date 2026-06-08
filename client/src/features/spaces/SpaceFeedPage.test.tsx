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

import { archivedCopy } from './SpaceFeedPage'

describe('SpaceFeedPage', () => {
  it('module exports a default component', async () => {
    const mod = await import('./SpaceFeedPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  }, 20000)
})

describe('archivedCopy', () => {
  it('returns null when the space is not archived', () => {
    expect(archivedCopy(false, null)).toBeNull()
    expect(archivedCopy(undefined, null)).toBeNull()
  })

  it('uses the "dissolved by its owner" wording for a dissolved archive', () => {
    const copy = archivedCopy(true, 'dissolved')
    expect(copy).not.toBeNull()
    expect(copy!.title).toMatch(/dissolved by its owner/i)
    // It can't be revived — no "ask an admin to unarchive" line.
    expect(`${copy!.title} ${copy!.body} ${copy!.empty}`).not.toMatch(/unarchive/i)
  })

  it('uses the "no longer a member" wording for a removed archive', () => {
    const copy = archivedCopy(true, 'removed')
    expect(copy).not.toBeNull()
    expect(copy!.title).toMatch(/no longer a member/i)
    expect(`${copy!.title} ${copy!.body} ${copy!.empty}`).not.toMatch(/unarchive/i)
  })

  it('keeps the "until an admin unarchives it" wording for a plain admin archive', () => {
    const copy = archivedCopy(true, null)
    expect(copy).not.toBeNull()
    expect(`${copy!.body} ${copy!.empty}`).toMatch(/unarchive/i)
  })
})
