import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, screen, cleanup, waitFor } from '@testing-library/preact'

const apiGet = vi.fn()
const apiPatch = vi.fn()
const apiDelete = vi.fn()

vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    patch: (...a: unknown[]) => apiPatch(...a),
    post: vi.fn(),
    delete: (...a: unknown[]) => apiDelete(...a),
  },
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
}))

vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

beforeEach(() => {
  apiGet.mockReset().mockResolvedValue({ users: [] })
  apiPatch.mockReset().mockResolvedValue({})
  apiDelete.mockReset().mockResolvedValue({})
  cleanup()
})

const _conn = (over: Partial<Record<string, unknown>> = {}) => ({
  instance_id: 'z7k63zfi',
  display_name: 'z7k63zfi',
  federated_display_name: 'z7k63zfi',
  local_alias: null,
  status: 'confirmed',
  inbox_url: 'https://x/wh/abc',
  intro_relay_enabled: true,
  unreachable_since: null,
  paired_at: '2026-05-18T10:00:00+00:00',
  ...over,
})

describe('ConnectionDetail — alias rename row', () => {
  it('module exports exist', async () => {
    const mod = await import('./ConnectionDetail')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  it('shows the federated name in the placeholder and hint when no alias is set', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn() as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    const input = await screen.findByLabelText('Display this household as') as HTMLInputElement
    expect(input.placeholder).toBe('z7k63zfi')
    // Hint mentions the peer's advertised name when no alias is set.
    const hint = document.querySelector('.sh-connection-alias__hint')
    expect(hint?.textContent).toMatch(/They advertise themselves as "z7k63zfi"/)
  })

  it('Save button is disabled until the alias differs from the persisted value', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ local_alias: 'Brother' }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    const save = (await screen.findAllByText('Save'))[0] as HTMLButtonElement
    expect(save.disabled).toBe(true)
    const input = screen.getByLabelText('Display this household as') as HTMLInputElement
    fireEvent.input(input, { target: { value: 'Brother\'s house' } })
    expect(save.disabled).toBe(false)
  })

  it('Save PATCHes /api/pairing/connections/{id}/alias with the new value', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    const onAliasSaved = vi.fn()
    render(
      <ConnectionDetail
        conn={_conn() as any}
        onClose={() => {}}
        onRevoke={() => {}}
        onAliasSaved={onAliasSaved}
      />,
    )
    const input = await screen.findByLabelText('Display this household as') as HTMLInputElement
    fireEvent.input(input, { target: { value: "Brother's house" } })
    fireEvent.click((screen.getAllByText('Save'))[0])
    await new Promise(r => setTimeout(r, 0))
    expect(apiPatch).toHaveBeenCalledWith(
      '/api/pairing/connections/z7k63zfi/alias',
      { alias: "Brother's house" },
    )
    expect(onAliasSaved).toHaveBeenCalledTimes(1)
  })

  it('whitespace-only alias clears (posts null)', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ local_alias: 'OldName' }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    const input = await screen.findByLabelText('Display this household as') as HTMLInputElement
    fireEvent.input(input, { target: { value: '   ' } })
    fireEvent.click((screen.getAllByText('Save'))[0])
    await new Promise(r => setTimeout(r, 0))
    expect(apiPatch).toHaveBeenCalledWith(
      '/api/pairing/connections/z7k63zfi/alias',
      { alias: null },
    )
  })

  it('Enter key submits the alias too', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn() as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    const input = await screen.findByLabelText('Display this household as') as HTMLInputElement
    fireEvent.input(input, { target: { value: 'Mom' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await new Promise(r => setTimeout(r, 0))
    expect(apiPatch).toHaveBeenCalledWith(
      '/api/pairing/connections/z7k63zfi/alias',
      { alias: 'Mom' },
    )
  })
})

describe('Transport row', () => {
  it('shows Direct (WebRTC DataChannel) for rtc', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ transport: 'rtc' }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.getByText(/Direct \(WebRTC DataChannel\)/i)).toBeTruthy()
  })

  it('shows HTTPS inbox (fallback) for https', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ transport: 'https' }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.getByText(/HTTPS inbox \(fallback\)/i)).toBeTruthy()
  })

  it('omits the Transport row when transport is null', async () => {
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ transport: null }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    expect(screen.queryByText(/Transport/i)).toBeNull()
  })
})

describe('DM path row', () => {
  it('renders when /transport-detail returns a recent relay', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url.includes('/transport-detail')) {
        return Promise.resolve({ last_relay: { via: 'peer-relay', ts: '2026-05-19T07:30:00+00:00' } })
      }
      return Promise.resolve({ users: [] })
    })
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ transport: 'https' }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    await waitFor(() => {
      expect(screen.getByText(/Last DM took the relay path/i)).toBeTruthy()
      expect(screen.getByText(/peer-relay/)).toBeTruthy()
    })
  })

  it('does not render when /transport-detail returns null', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url.includes('/transport-detail')) {
        return Promise.resolve({ last_relay: null })
      }
      return Promise.resolve({ users: [] })
    })
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ transport: 'rtc' }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    await waitFor(() =>
      expect(screen.queryByText(/Last DM took the relay path/i)).toBeNull(),
    )
  })

  it('hides the DM path row silently on fetch error', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url.includes('/transport-detail')) {
        return Promise.reject(new Error('Network error'))
      }
      return Promise.resolve({ users: [] })
    })
    const { ConnectionDetail } = await import('./ConnectionDetail')
    render(
      <ConnectionDetail
        conn={_conn({ transport: 'https' }) as any}
        onClose={() => {}}
        onRevoke={() => {}}
      />,
    )
    // The Transport row should still render — proves the panel didn't crash:
    expect(screen.getByText(/HTTPS inbox \(fallback\)/i)).toBeTruthy()
    expect(screen.queryByText(/Last DM took the relay path/i)).toBeNull()
  })
})
