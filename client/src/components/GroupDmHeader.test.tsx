import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { GroupDmHeader } from './GroupDmHeader'

vi.mock('@/api', () => ({ api: { post: vi.fn() } }))

describe('GroupDmHeader', () => {
  it('module exports exist', () => {
    expect(GroupDmHeader).toBeTruthy()
  })

  // Regression guard for the render-body `signal()` state-loss bug: the
  // members toggle is local component state, so it MUST survive the re-render
  // its own click triggers. A `signal(false)` created in the render body is a
  // fresh signal every render → the toggle reverts to false on the re-render
  // and the list never opens. `useSignal` keeps it stable across renders.
  it('keeps the members list open after toggling (state persists across re-render)', () => {
    const members = [
      { username: 'ada', display_name: 'Ada' },
      { username: 'bob', display_name: 'Bob' },
    ]
    const { getByText, queryByText } = render(
      <GroupDmHeader conversationId="c1" name="Crew" members={members} onUpdate={() => {}} />,
    )
    // Closed initially.
    expect(queryByText('+ Add member')).toBeNull()
    // Open it.
    fireEvent.click(getByText('Members'))
    // With the bug this reverted to closed on re-render; the list must stay open.
    expect(getByText('+ Add member')).toBeTruthy()
    expect(getByText('Ada')).toBeTruthy()
    expect(getByText('Hide')).toBeTruthy()
  })
})
