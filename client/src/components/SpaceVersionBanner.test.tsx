import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, act } from '@testing-library/preact'

const { apiGet } = vi.hoisted(() => ({ apiGet: vi.fn() }))
vi.mock('@/api', () => ({ api: { get: apiGet } }))

import { SpaceVersionBanner } from './SpaceVersionBanner'

function compat(over: Record<string, unknown> = {}) {
  return {
    ours: 18,
    min_member_proto_version: 13,
    lagging_features: ['Media DataChannel', 'Remote admin actions'],
    behind_members: [
      {
        instance_id: 'peer-13',
        display_name: "Brother's house",
        proto_version: 13,
        lacking_features: ['Media DataChannel', 'Remote admin actions'],
      },
    ],
    ...over,
  }
}

describe('SpaceVersionBanner', () => {
  beforeEach(() => {
    apiGet.mockReset()
  })

  it('renders nothing when no features lag', async () => {
    apiGet.mockResolvedValue(
      compat({ lagging_features: [], behind_members: [] }),
    )
    const { container } = render(<SpaceVersionBanner spaceId="s1" />)
    await act(async () => {
      await Promise.resolve()
    })
    expect(container.textContent).toBe('')
  })

  it('renders the lagging features and the behind household + version', async () => {
    apiGet.mockResolvedValue(compat())
    const { container } = render(<SpaceVersionBanner spaceId="s1" />)
    await waitFor(() =>
      expect(container.textContent).toContain(
        'Some members are on an older version',
      ),
    )
    expect(container.textContent).toContain('Media DataChannel')
    expect(container.textContent).toContain('Remote admin actions')
    expect(container.textContent).toContain("Brother's house (v13)")
  })

  it('renders nothing when the request rejects (best-effort 403)', async () => {
    apiGet.mockRejectedValue(new Error('forbidden'))
    const { container } = render(<SpaceVersionBanner spaceId="s1" />)
    await act(async () => {
      await Promise.resolve()
    })
    expect(container.textContent).toBe('')
  })
})
