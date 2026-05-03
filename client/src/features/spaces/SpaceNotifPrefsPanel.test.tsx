import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

vi.mock('@/api', () => {
  const m = { get: vi.fn(), put: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
  return { api: m }
})
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { SpaceNotifPrefsPanel } from './SpaceNotifPrefsPanel'
import { api } from '@/api'

const apiMock = api as unknown as {
  get: ReturnType<typeof vi.fn>
  put: ReturnType<typeof vi.fn>
}

describe('SpaceNotifPrefsPanel', () => {
  beforeEach(() => {
    apiMock.get.mockReset()
    apiMock.put.mockReset()
  })

  it('renders the three radio options after the level loads', async () => {
    apiMock.get.mockResolvedValueOnce({ level: 'all' })
    const { findAllByRole } = render(<SpaceNotifPrefsPanel spaceId="s1" />)
    const radios = await findAllByRole('radio')
    expect(radios.length).toBe(3)
  })

  it('PUTs the new level when a different option is picked', async () => {
    apiMock.get.mockResolvedValueOnce({ level: 'all' })
    apiMock.put.mockResolvedValueOnce({ level: 'mentions' })
    const { findAllByRole } = render(<SpaceNotifPrefsPanel spaceId="s1" />)
    const radios = await findAllByRole('radio')
    // radios are in order: all, mentions, muted (matches LEVELS order)
    fireEvent.click(radios[1])
    await new Promise(r => setTimeout(r, 0))
    expect(apiMock.put).toHaveBeenCalledWith(
      '/api/spaces/s1/notif-prefs',
      { level: 'mentions' },
    )
  })

  it('rolls back the optimistic flip when the PUT fails', async () => {
    apiMock.get.mockResolvedValueOnce({ level: 'all' })
    apiMock.put.mockRejectedValueOnce(new Error('boom'))
    const { findAllByRole } = render(<SpaceNotifPrefsPanel spaceId="s1" />)
    const radios = await findAllByRole('radio')
    fireEvent.click(radios[2])  // muted
    await new Promise(r => setTimeout(r, 0))
    // After the rejection the original ``all`` radio is selected again.
    expect((radios[0] as HTMLInputElement).checked).toBe(true)
    expect((radios[2] as HTMLInputElement).checked).toBe(false)
  })
})
