import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  render, fireEvent, act, cleanup, waitFor,
} from '@testing-library/preact'

const aliasOpenSpy = vi.fn()
vi.mock('@/components/AliasDialog', () => ({
  openAliasDialog: aliasOpenSpy,
  AliasDialog: () => null,
}))

const { FriendActionSheet, openFriendActions } = await import('./FriendActionSheet')

beforeEach(() => {
  aliasOpenSpy.mockReset()
})

afterEach(() => {
  cleanup()
})

function bob(opts: { personal_alias?: string | null } = {}): Parameters<typeof openFriendActions>[0] {
  return {
    user_id: 'uid-bob',
    username: 'bob',
    display_name: 'Bob (Beta House)',
    personal_alias: opts.personal_alias ?? null,
    picture_url: null,
    household: 'Beta House',
    is_local: false,
  }
}

describe('FriendActionSheet', () => {
  it('renders nothing until openFriendActions is called', () => {
    const { container } = render(
      <FriendActionSheet onStartDm={vi.fn()} />,
    )
    expect(
      container.querySelector('[data-testid="friend-action-message"]'),
    ).toBeNull()
  })

  it('shows "Set nickname" when no alias is set', async () => {
    const { container } = render(<FriendActionSheet onStartDm={vi.fn()} />)
    await act(async () => { openFriendActions(bob()) })
    await waitFor(() => {
      const btn = container.querySelector('[data-testid="friend-action-rename"]')
      expect(btn?.textContent).toContain('Set nickname')
    })
  })

  it('shows "Edit nickname" + the real name when an alias is already set', async () => {
    const { container } = render(<FriendActionSheet onStartDm={vi.fn()} />)
    await act(async () => {
      openFriendActions(bob({ personal_alias: 'Brother' }))
    })
    await waitFor(() => {
      const btn = container.querySelector('[data-testid="friend-action-rename"]')
      expect(btn?.textContent).toContain('Edit nickname')
    })
    expect(container.textContent).toContain('Their name: Bob (Beta House)')
  })

  it('Message button calls onStartDm with the target and closes the sheet', async () => {
    const onStartDm = vi.fn().mockResolvedValue(undefined)
    const { container } = render(<FriendActionSheet onStartDm={onStartDm} />)
    await act(async () => { openFriendActions(bob()) })
    const btn = container.querySelector(
      '[data-testid="friend-action-message"]',
    ) as HTMLButtonElement
    await act(async () => { fireEvent.click(btn) })
    await waitFor(() => {
      expect(onStartDm).toHaveBeenCalledWith(
        expect.objectContaining({ user_id: 'uid-bob', username: 'bob' }),
      )
    })
    // After resolve, the sheet should be gone.
    expect(
      container.querySelector('[data-testid="friend-action-message"]'),
    ).toBeNull()
  })

  it('Rename button opens AliasDialog with the right args and closes the sheet', async () => {
    const onChanged = vi.fn()
    const { container } = render(
      <FriendActionSheet
        onStartDm={vi.fn()}
        onAliasChanged={onChanged}
      />,
    )
    await act(async () => {
      openFriendActions(bob({ personal_alias: 'Brother' }))
    })
    const btn = container.querySelector(
      '[data-testid="friend-action-rename"]',
    ) as HTMLButtonElement
    await act(async () => { fireEvent.click(btn) })
    expect(aliasOpenSpy).toHaveBeenCalledWith(expect.objectContaining({
      targetUserId: 'uid-bob',
      globalDisplayName: 'Bob (Beta House)',
      currentAlias: 'Brother',
    }))
    // Sheet closed.
    expect(
      container.querySelector('[data-testid="friend-action-rename"]'),
    ).toBeNull()
    // Simulate the AliasDialog firing its onSave callback.
    const args = aliasOpenSpy.mock.calls[0][0]
    args.onSave('Bro')
    expect(onChanged).toHaveBeenCalledWith('uid-bob', 'Bro')
  })
})
