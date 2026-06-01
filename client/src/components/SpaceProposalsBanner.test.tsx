import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

const { apiGet, apiPost } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
}))
let wsHandler: ((e: unknown) => void) | null = null
vi.mock('@/api', () => ({ api: { get: apiGet, post: apiPost } }))
vi.mock('@/ws', () => ({
  ws: {
    on: (_t: string, h: (e: unknown) => void) => {
      wsHandler = h
      return () => {
        wsHandler = null
      }
    },
  },
}))
vi.mock('./Toast', () => ({ showToast: vi.fn() }))

import { SpaceProposalsBanner } from './SpaceProposalsBanner'

function proposal(over: Record<string, unknown> = {}) {
  return {
    id: 'p1',
    action: 'dissolve',
    status: 'pending',
    approvals: 1,
    needed: 2,
    total_admins: 2,
    proposed_by_user: 'alice',
    ...over,
  }
}

describe('SpaceProposalsBanner', () => {
  beforeEach(() => {
    apiGet.mockReset()
    apiPost.mockReset()
    wsHandler = null
  })

  it('renders a pending dissolve proposal with the tally', async () => {
    apiGet.mockResolvedValue({ proposals: [proposal()] })
    const { container } = render(
      <SpaceProposalsBanner spaceId="s1" canVote={true} />,
    )
    await waitFor(() =>
      expect(container.textContent).toContain('Admin approval needed'),
    )
    expect(container.textContent).toContain('permanently delete this space')
    expect(container.textContent).toContain('1 of 2')
  })

  it('shows Approve/Reject only when the viewer can vote', async () => {
    apiGet.mockResolvedValue({ proposals: [proposal()] })
    const { container, rerender } = render(
      <SpaceProposalsBanner spaceId="s1" canVote={false} />,
    )
    await waitFor(() =>
      expect(container.textContent).toContain('Waiting for the space admins'),
    )
    expect(container.querySelector('button')).toBeNull()

    apiGet.mockResolvedValue({ proposals: [proposal()] })
    rerender(<SpaceProposalsBanner spaceId="s1" canVote={true} />)
    await waitFor(() => expect(container.querySelector('button')).not.toBeNull())
  })

  it('POSTs a vote on Approve', async () => {
    apiGet.mockResolvedValue({ proposals: [proposal()] })
    apiPost.mockResolvedValue({})
    const { getByText } = render(
      <SpaceProposalsBanner spaceId="s1" canVote={true} />,
    )
    await waitFor(() => getByText('Approve'))
    fireEvent.click(getByText('Approve'))
    await waitFor(() =>
      expect(apiPost).toHaveBeenCalledWith(
        '/api/spaces/s1/proposals/p1/vote',
        { approve: true },
      ),
    )
  })

  it('drops the banner when a WS frame resolves the proposal', async () => {
    apiGet.mockResolvedValue({ proposals: [proposal()] })
    const { container } = render(
      <SpaceProposalsBanner spaceId="s1" canVote={true} />,
    )
    await waitFor(() =>
      expect(container.textContent).toContain('Admin approval needed'),
    )
    wsHandler?.({ space_id: 's1', proposal: proposal({ status: 'executed' }) })
    await waitFor(() =>
      expect(container.textContent).not.toContain('Admin approval needed'),
    )
  })
})
