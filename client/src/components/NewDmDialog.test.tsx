import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'

beforeEach(() => {
  vi.resetModules()
})

describe('NewDmDialog', () => {
  it('module exports exist', async () => {
    vi.doMock('@/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
    vi.doMock('@/store/auth', () => ({ currentUser: { value: null } }))
    const mod = await import('./NewDmDialog')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  it('omits the current user from the recipient list', async () => {
    const get = vi.fn(async () => ([
      { username: 'pascal', display_name: 'Pascal' },
      { username: 'maria',  display_name: 'Maria'  },
      { username: 'lina',   display_name: 'Lina'   },
    ]))
    vi.doMock('@/api', () => ({ api: { get, post: vi.fn() } }))
    vi.doMock('@/store/auth', () => ({
      currentUser: { value: { username: 'pascal', display_name: 'Pascal' } },
    }))
    const { NewDmDialog, openNewDm } = await import('./NewDmDialog')
    openNewDm()
    // Wait a tick for the api.get to resolve and the signal to settle.
    await new Promise((r) => setTimeout(r, 0))
    const { findByRole, queryByText } = render(<NewDmDialog />)
    await findByRole('dialog')
    expect(queryByText(/Maria/)).toBeTruthy()
    expect(queryByText(/Lina/)).toBeTruthy()
    // The current user must not appear as a self-DM target.
    expect(queryByText(/Pascal/)).toBeNull()
  })

  it('renders the group-name field only after a 2nd member is picked', async () => {
    const get = vi.fn(async () => ([
      { username: 'maria', display_name: 'Maria' },
      { username: 'lina',  display_name: 'Lina'  },
    ]))
    vi.doMock('@/api', () => ({ api: { get, post: vi.fn() } }))
    vi.doMock('@/store/auth', () => ({
      currentUser: { value: { username: 'pascal', display_name: 'Pascal' } },
    }))
    const { NewDmDialog, openNewDm } = await import('./NewDmDialog')
    openNewDm()
    await new Promise((r) => setTimeout(r, 0))
    const { container, findByRole, queryByText } = render(<NewDmDialog />)
    await findByRole('dialog')
    // No group-name field with zero picks.
    expect(queryByText(/Group name/)).toBeNull()

    // Pick Maria — still no group-name field (1 pick = 1:1 DM).
    const rows = container.querySelectorAll('.sh-newdm-row')
    ;(rows[0] as HTMLElement).click()
    await new Promise((r) => setTimeout(r, 0))
    expect(queryByText(/Group name/)).toBeNull()

    // Pick Lina too — 2 picks = group, name field appears.
    ;(rows[1] as HTMLElement).click()
    await new Promise((r) => setTimeout(r, 0))
    expect(queryByText(/Group name/)).toBeTruthy()
  })

  it('routes a 1-pick to /dm and a 2-pick to /group', async () => {
    const post = vi.fn(async (url: string) => {
      if (url.endsWith('/group')) return { id: 'cgroup', type: 'group_dm' }
      return { id: 'cdm', type: 'dm' }
    })
    const get = vi.fn(async () => ([
      { username: 'maria', display_name: 'Maria' },
      { username: 'lina',  display_name: 'Lina'  },
    ]))
    vi.doMock('@/api', () => ({ api: { get, post } }))
    vi.doMock('@/store/auth', () => ({
      currentUser: { value: { username: 'pascal', display_name: 'Pascal' } },
    }))
    const { NewDmDialog, openNewDm } = await import('./NewDmDialog')

    // 1-pick path → /api/conversations/dm
    openNewDm()
    await new Promise((r) => setTimeout(r, 0))
    const { container, findByRole, getByText, unmount } = render(<NewDmDialog />)
    await findByRole('dialog')
    const rows = container.querySelectorAll('.sh-newdm-row')
    ;(rows[0] as HTMLElement).click()
    await new Promise((r) => setTimeout(r, 0))
    ;(getByText(/^Start$/) as HTMLElement).click()
    await new Promise((r) => setTimeout(r, 0))
    expect(post).toHaveBeenCalledWith(
      '/api/conversations/dm',
      { username: 'maria' },
    )
    unmount()

    // 2-pick path → /api/conversations/group with members[]
    post.mockClear()
    openNewDm()
    await new Promise((r) => setTimeout(r, 0))
    const second = render(<NewDmDialog />)
    await second.findByRole('dialog')
    const rows2 = second.container.querySelectorAll('.sh-newdm-row')
    ;(rows2[0] as HTMLElement).click()
    ;(rows2[1] as HTMLElement).click()
    await new Promise((r) => setTimeout(r, 0))
    ;(second.getByText(/^Start group/) as HTMLElement).click()
    await new Promise((r) => setTimeout(r, 0))
    expect(post).toHaveBeenCalledWith(
      '/api/conversations/group',
      { members: ['maria', 'lina'] },
    )
  })
})
