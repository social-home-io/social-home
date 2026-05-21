import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  render, fireEvent, waitFor, act, cleanup,
} from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn() },
}))

const { api } = await import('@/api') as unknown as {
  api: { get: ReturnType<typeof vi.fn>; post: ReturnType<typeof vi.fn> }
}
const { RemoteInviteDialog, openRemoteInviteDialog } = await import('./RemoteInviteDialog')

const FRIENDS_PAYLOAD = {
  instance: {
    instance_id: 'self-instance',
    display_name: 'Alpha House',
    members: [
      { user_id: 'uid-alice', display_name: 'Alice (Alpha)', last_seen_at: null },
    ],
  },
  households: [
    {
      instance_id: 'beta-instance',
      display_name: 'Beta House',
      status: 'confirmed',
      reachable: true,
      members: [
        {
          user_id: 'uid-bob',
          instance_id: 'beta-instance',
          remote_username: 'bob',
          display_name: 'Bob (Beta)',
          last_seen_at: new Date(Date.now() - 5 * 60_000).toISOString(),
        },
        {
          user_id: 'uid-carol',
          instance_id: 'beta-instance',
          remote_username: 'carol',
          display_name: 'Carol (Beta)',
          last_seen_at: null,
        },
      ],
    },
    {
      instance_id: 'gamma-instance',
      display_name: 'Gamma House',
      status: 'confirmed',
      reachable: false,
      members: [
        {
          user_id: 'uid-dave',
          instance_id: 'gamma-instance',
          remote_username: 'dave',
          display_name: 'Dave (Gamma)',
          last_seen_at: null,
        },
      ],
    },
    {
      // Pending household — should NOT appear in the picker.
      instance_id: 'pending-instance',
      display_name: 'Pending',
      status: 'pending',
      reachable: false,
      members: [
        { user_id: 'uid-x', instance_id: 'pending-instance',
          display_name: 'Pending User', last_seen_at: null },
      ],
    },
  ],
}

beforeEach(() => {
  api.get.mockReset()
  api.post.mockReset()
})

afterEach(() => {
  cleanup()
})

async function renderAndOpen() {
  api.get.mockResolvedValueOnce(FRIENDS_PAYLOAD)
  const result = render(<RemoteInviteDialog />)
  await act(async () => { openRemoteInviteDialog('space-uuid-1') })
  await waitFor(() => {
    expect(result.container.querySelector('[data-testid="remote-invite-search"]'))
      .not.toBeNull()
  })
  return result
}

describe('RemoteInviteDialog', () => {
  it('flattens /api/friends households[].members[] into the picker', async () => {
    const { container } = await renderAndOpen()
    expect(api.get).toHaveBeenCalledWith('/api/friends')
    // Three confirmed members across two households; the pending
    // household's user must NOT appear.
    const rows = container.querySelectorAll('.sh-remote-invite-row')
    expect(rows.length).toBe(3)
    const names = [...rows].map(r =>
      r.querySelector('.sh-remote-invite-row__name')?.textContent)
    expect(names).toEqual(['Bob (Beta)', 'Carol (Beta)', 'Dave (Gamma)'])
  })

  it('filters the list by name and by household', async () => {
    const { container } = await renderAndOpen()
    const search = container.querySelector('[data-testid="remote-invite-search"]') as HTMLInputElement
    // Name filter.
    await act(async () => {
      fireEvent.input(search, { target: { value: 'carol' } })
    })
    expect(container.querySelectorAll('.sh-remote-invite-row').length).toBe(1)
    // Household filter.
    await act(async () => {
      fireEvent.input(search, { target: { value: 'gamma' } })
    })
    expect(container.querySelectorAll('.sh-remote-invite-row').length).toBe(1)
  })

  it('POSTs the picked row\'s instance_id + user_id on submit', async () => {
    api.post.mockResolvedValueOnce({})
    const { container, getByText } = await renderAndOpen()
    const bobRow = container.querySelector(
      '[data-testid="remote-invite-row-uid-bob"]',
    ) as HTMLButtonElement
    await act(async () => { fireEvent.click(bobRow) })
    await act(async () => { fireEvent.click(getByText('Send invite')) })
    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        '/api/spaces/space-uuid-1/remote-invites',
        {
          invitee_instance_id: 'beta-instance',
          invitee_user_id: 'uid-bob',
        },
      )
    })
  })

  it('shows last-seen freshness per row', async () => {
    const { container } = await renderAndOpen()
    const bobMeta = container
      .querySelector('[data-testid="remote-invite-row-uid-bob"]')
      ?.querySelector('.sh-remote-invite-row__meta')?.textContent
    // Bob was last seen 5 minutes ago.
    expect(bobMeta).toContain('Beta House')
    expect(bobMeta).toContain('5 min ago')
    const carolMeta = container
      .querySelector('[data-testid="remote-invite-row-uid-carol"]')
      ?.querySelector('.sh-remote-invite-row__meta')?.textContent
    expect(carolMeta).toContain('never seen')
  })

  it('renders the no-paired-households empty state', async () => {
    api.get.mockResolvedValueOnce({
      instance: FRIENDS_PAYLOAD.instance,
      households: [],
    })
    const result = render(<RemoteInviteDialog />)
    await act(async () => { openRemoteInviteDialog('space-uuid-empty') })
    await waitFor(() => {
      expect(result.container.textContent)
        .toContain('paired household with visible members')
    })
  })
})
