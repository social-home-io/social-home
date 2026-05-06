import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'
import { LocationProvider } from 'preact-iso'

const apiGet  = vi.fn()
const apiPost = vi.fn()

vi.mock('@/api', () => ({
  api: {
    get:  (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
  },
}))

vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { active } from '@/store/calls'

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  apiPost.mockReset()
  active.value = []
})

async function renderTab() {
  const { default: CallsTab } = await import('./CallsTab')
  return render(
    <LocationProvider>
      <CallsTab />
    </LocationProvider>,
  )
}

describe('CallsTab', () => {
  it('shows the empty state when there are no active calls', async () => {
    apiGet.mockResolvedValue([])
    const { findByText } = await renderTab()
    expect(await findByText('No active calls')).toBeTruthy()
  })

  it('renders an active-call row from the active signal', async () => {
    apiGet.mockResolvedValue([
      { call_id: 'c-1', status: 'in_progress', caller: 'Alice',
        callee: 'Bob', call_type: 'audio', created_at: 0 },
    ])
    const { findByText } = await renderTab()
    expect(await findByText('In progress')).toBeTruthy()
    expect(await findByText(/Alice/)).toBeTruthy()
  })

  it('hangs up via /api/calls/{id}/hangup when the button is clicked', async () => {
    apiGet.mockResolvedValueOnce([
      { call_id: 'c-1', status: 'in_progress', caller: 'Alice',
        callee: 'Bob', call_type: 'audio', created_at: 0 },
    ])
    apiPost.mockResolvedValue({})
    apiGet.mockResolvedValueOnce([])  // second load after hangup
    const { findAllByText } = await renderTab()
    const hangUpButtons = await findAllByText('Hang up')
    fireEvent.click(hangUpButtons[0])
    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith('/api/calls/c-1/hangup', {})
    })
  })
})
