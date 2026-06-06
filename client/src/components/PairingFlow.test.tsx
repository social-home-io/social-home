import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, screen, cleanup } from '@testing-library/preact'

const apiPost = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: vi.fn(),
    post: (...args: unknown[]) => apiPost(...args),
    patch: vi.fn(),
    delete: vi.fn(),
  },
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
}))

// WS event handlers are captured so tests can fire synthetic events.
const wsHandlers: Record<string, ((e: { data: unknown }) => void)[]> = {}
const wsOnMock = vi.fn((event: string, handler: (e: { data: unknown }) => void) => {
  if (!wsHandlers[event]) wsHandlers[event] = []
  wsHandlers[event].push(handler)
  return () => {
    wsHandlers[event] = (wsHandlers[event] || []).filter(h => h !== handler)
  }
})
function fireWs(event: string, data: unknown) {
  for (const h of wsHandlers[event] ?? []) h({ data })
}

vi.mock('@/ws', () => ({
  ws: { on: (...args: Parameters<typeof wsOnMock>) => wsOnMock(...args) },
}))

// preact-iso: the success-dialog CTA navigates via the base/ingress-aware
// client router (loc.route), never a raw location.assign. Mock useLocation
// so the test can assert the route call.
const routeSpy = vi.fn()
vi.mock('preact-iso', () => ({
  useLocation: () => ({ route: routeSpy, url: '/' }),
}))

const showToastSpy = vi.fn()
vi.mock('./Toast', () => ({
  showToast: (...args: unknown[]) => showToastSpy(...args),
}))

vi.mock('@/i18n/i18n', () => ({
  t: (key: string) => key,
  locale: { value: 'en' },
  setLocale: vi.fn(),
}))

vi.mock('./ShareHomeToggle', () => ({
  ShareHomeToggle: ({ instanceId, peerName }: { instanceId: string; peerName: string }) => (
    <div data-testid="share-home-toggle"
         data-instance-id={instanceId}
         data-peer-name={peerName} />
  ),
}))

// Trivial QR mock — the unit test isn't here to verify the QR
// library, just the surrounding flow.
vi.mock('qrcode', () => ({
  default: { toDataURL: vi.fn().mockResolvedValue('data:image/png;base64,abc') },
}))

const writeText = vi.fn().mockResolvedValue(undefined)
Object.assign(navigator, { clipboard: { writeText } })

beforeEach(() => {
  vi.resetModules()
  apiPost.mockReset()
  routeSpy.mockReset()
  showToastSpy.mockReset()
  writeText.mockReset().mockResolvedValue(undefined)
  // Clear captured WS handlers so stale handlers from prior tests don't fire.
  for (const k of Object.keys(wsHandlers)) delete wsHandlers[k]
  cleanup()
})

describe('PairingFlow — module surface', () => {
  it('module exports', async () => {
    const mod = await import('./PairingFlow')
    expect(typeof mod.openPairing).toBe('function')
    expect(typeof mod.PairingFlow).toBe('function')
    expect(typeof mod.buildPairingCode).toBe('function')
  })
})

describe('PairingFlow — socialhome://pair code round-trip', () => {
  it('encodes the payload as a single-line socialhome://pair#<b64> URL', async () => {
    const { buildPairingCode } = await import('./PairingFlow')
    const payload = JSON.stringify({
      token: 'tok-123',
      identity_pk: 'ed25519:beef',
      dh_pk: 'x25519:cafe',
      inbox_url: 'https://alice.example/inbox',
      expires_at: '2026-05-18T10:00:00Z',
    })
    const code = buildPairingCode(payload)
    expect(code.startsWith('socialhome://pair#')).toBe(true)
    expect(code.includes('\n')).toBe(false)
    // Whole code should round-trip back through atob → JSON.parse.
    const fragment = code.slice('socialhome://pair#'.length)
    const padded = fragment.replace(/-/g, '+').replace(/_/g, '/')
        + '='.repeat((4 - (fragment.length % 4)) % 4)
    const decoded = atob(padded)
    expect(JSON.parse(decoded)).toEqual(JSON.parse(payload))
  })

  it('inviter side copies socialhome://pair# code on click', async () => {
    apiPost.mockResolvedValueOnce({
      token: 'tok-xyz',
      identity_pk: 'ed25519:bb',
      dh_pk: 'x25519:cc',
      inbox_url: 'https://h.example/inbox/abc',
      expires_at: '2026-05-18T10:05:00Z',
    })
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('household')
    await screen.findByText('pairing.role_show')
    fireEvent.click(screen.getByLabelText('pairing.role_show_aria'))
    const copyBtn = await screen.findByText('pairing.copy_code')
    fireEvent.click(copyBtn)
    await new Promise(r => setTimeout(r, 0))
    expect(writeText).toHaveBeenCalledTimes(1)
    const arg = writeText.mock.calls[0][0] as string
    expect(arg.startsWith('socialhome://pair#')).toBe(true)
  })
})

describe('PairingFlow — scanner paste decoding', () => {
  it('decodes a socialhome://pair# code paste and POSTs to /api/pairing/accept', async () => {
    const payload = {
      token: 'tok-zzz',
      identity_pk: 'ed25519:11',
      dh_pk: 'x25519:22',
      inbox_url: 'https://bob.example/inbox',
      expires_at: '2026-05-18T10:00:00Z',
    }
    const json = JSON.stringify(payload)
    const { buildPairingCode, PairingFlow, openPairing } = await import('./PairingFlow')
    const code = buildPairingCode(json)
    apiPost.mockResolvedValueOnce({ verification_code: '123456', token: 'tok-zzz' })

    render(<PairingFlow />)
    openPairing('household')
    await screen.findByText('pairing.role_scan')
    fireEvent.click(screen.getByLabelText('pairing.role_scan_aria'))

    // Method picker — click Paste code
    const pasteCard = await screen.findByText('pairing.method_paste')
    fireEvent.click(pasteCard)

    // Find the textarea + fill it
    const textarea = await screen.findByPlaceholderText('pairing.paste_placeholder')
    fireEvent.input(textarea, { target: { value: code } })

    // Submit
    fireEvent.click(screen.getByText('pairing.paste_submit'))

    await new Promise(r => setTimeout(r, 0))
    expect(apiPost).toHaveBeenCalledWith('/api/pairing/accept', payload)
  })

  it('back-compat: raw JSON paste still works', async () => {
    const payload = {
      token: 'legacy',
      identity_pk: 'ed25519:legacy',
      dh_pk: 'x25519:legacy',
      inbox_url: 'https://l.example/inbox',
    }
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    apiPost.mockResolvedValueOnce({ verification_code: '098765', token: 'legacy' })
    render(<PairingFlow />)
    openPairing('household')
    await screen.findByText('pairing.role_scan')
    fireEvent.click(screen.getByLabelText('pairing.role_scan_aria'))
    fireEvent.click(await screen.findByText('pairing.method_paste'))
    const textarea = await screen.findByPlaceholderText('pairing.paste_placeholder')
    fireEvent.input(textarea, { target: { value: JSON.stringify(payload) } })
    fireEvent.click(screen.getByText('pairing.paste_submit'))
    await new Promise(r => setTimeout(r, 0))
    expect(apiPost).toHaveBeenCalledWith('/api/pairing/accept', payload)
  })
})

describe('PairingFlow — GFS paste decoding', () => {
  it('parses socialhome://gfs-pair/{url}?token=… and POSTs {gfs_url, token}', async () => {
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    apiPost.mockResolvedValueOnce({})
    render(<PairingFlow />)
    openPairing('gfs')

    // gfs idle → click Add GFS
    const addBtn = await screen.findByText('gfs.add')
    fireEvent.click(addBtn)

    // Method picker
    fireEvent.click(await screen.findByText('pairing.method_paste'))

    const textarea = await screen.findByPlaceholderText('gfs.paste_placeholder')
    const code = 'socialhome://gfs-pair/https://gfs.example.com/?token=t-aabbcc'
    fireEvent.input(textarea, { target: { value: code } })
    fireEvent.click(screen.getByText('pairing.paste_submit'))

    await new Promise(r => setTimeout(r, 0))
    expect(apiPost).toHaveBeenCalledWith('/api/gfs/connections', {
      gfs_url: 'https://gfs.example.com',
      token: 't-aabbcc',
    })
  })

  it('rejects a malformed GFS code without hitting the backend', async () => {
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('gfs')
    fireEvent.click(await screen.findByText('gfs.add'))
    fireEvent.click(await screen.findByText('pairing.method_paste'))
    const textarea = await screen.findByPlaceholderText('gfs.paste_placeholder')
    fireEvent.input(textarea, { target: { value: 'not-a-code' } })
    fireEvent.click(screen.getByText('pairing.paste_submit'))
    await new Promise(r => setTimeout(r, 0))
    expect(apiPost).not.toHaveBeenCalled()
  })
})

describe('PairingFlow — configure-sharing step', () => {
  it('household success Done advances to configure-sharing', async () => {
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('household')

    // Fire the pairing.confirmed WS event to flip to 'success'.
    fireWs('pairing.confirmed', {
      instance_id: 'peer-abc123',
      display_name: 'Alice Home',
    })
    await new Promise(r => setTimeout(r, 0))

    // The success screen shows "pairing.success" and a Done button.
    await screen.findByText('pairing.success')
    fireEvent.click(screen.getByText('pairing.done'))
    await new Promise(r => setTimeout(r, 0))

    // We should now see the configure-sharing step.
    expect(screen.getByText('pairing.configure_sharing_title')).toBeTruthy()
    // ShareHomeToggle should be rendered with the just-paired peer's id.
    const toggle = screen.getByTestId('share-home-toggle')
    expect(toggle.getAttribute('data-instance-id')).toBe('peer-abc123')
    expect(toggle.getAttribute('data-peer-name')).toBe('Alice Home')
  })

  it('configure-sharing Done closes the wizard', async () => {
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('household')

    // Drive to success via WS event.
    fireWs('pairing.confirmed', {
      instance_id: 'peer-xyz',
      display_name: 'Bob Home',
    })
    await new Promise(r => setTimeout(r, 0))
    await screen.findByText('pairing.success')

    // Advance to configure-sharing.
    fireEvent.click(screen.getByText('pairing.done'))
    await new Promise(r => setTimeout(r, 0))
    await screen.findByText('pairing.configure_sharing_title')

    // Click Done on configure-sharing — wizard should close.
    fireEvent.click(screen.getByText('pairing.done'))
    await new Promise(r => setTimeout(r, 0))

    // Modal is closed — success title is no longer visible.
    expect(screen.queryByText('pairing.configure_sharing_title')).toBeNull()
    expect(screen.queryByText('pairing.success')).toBeNull()
  })

  it('GFS success path is unchanged — Done closes directly without configure-sharing', async () => {
    apiPost.mockResolvedValueOnce({})
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('gfs')

    fireEvent.click(await screen.findByText('gfs.add'))
    fireEvent.click(await screen.findByText('pairing.method_paste'))
    const textarea = await screen.findByPlaceholderText('gfs.paste_placeholder')
    const code = 'socialhome://gfs-pair/https://gfs.example.com/?token=tok-gfs'
    fireEvent.input(textarea, { target: { value: code } })
    fireEvent.click(screen.getByText('pairing.paste_submit'))
    await new Promise(r => setTimeout(r, 0))

    // GFS success — Done should close, not go to configure-sharing.
    await screen.findByText('gfs.connected')
    fireEvent.click(screen.getByText('pairing.done'))
    await new Promise(r => setTimeout(r, 0))

    expect(screen.queryByText('pairing.configure_sharing_title')).toBeNull()
  })
})

describe('PairingFlow — GFS pending-approval success dialog', () => {
  async function connectGfs(status: string) {
    apiPost.mockResolvedValueOnce({
      id: 'gfs-1',
      gfs_instance_id: 'i1',
      display_name: 'Town GFS',
      inbox_url: 'https://gfs.example.com',
      status,
      paired_at: '2026-06-06T00:00:00+00:00',
      published_space_count: 0,
    })
    const { PairingFlow, openPairing } = await import('./PairingFlow')
    render(<PairingFlow />)
    openPairing('gfs')
    fireEvent.click(await screen.findByText('gfs.add'))
    fireEvent.click(await screen.findByText('pairing.method_paste'))
    const textarea = await screen.findByPlaceholderText('gfs.paste_placeholder')
    const code = 'socialhome://gfs-pair/https://gfs.example.com/?token=tok-gfs'
    fireEvent.input(textarea, { target: { value: code } })
    fireEvent.click(screen.getByText('pairing.paste_submit'))
    await new Promise(r => setTimeout(r, 0))
  }

  it('pending: shows the pending heading/message and only Done (no publish CTA)', async () => {
    await connectGfs('pending')

    // Pending copy, not the "Connected!" copy.
    expect(screen.getByText('gfs.connected_pending')).toBeTruthy()
    expect(screen.getByText('gfs.pending_approval')).toBeTruthy()
    expect(screen.queryByText('gfs.connected')).toBeNull()

    // Only the Done button — no "Open public sharing settings".
    expect(screen.getByText('pairing.done')).toBeTruthy()
    expect(screen.queryByText('gfs.open_publishing')).toBeNull()

    // Neutral/info toast, not the green "connected" toast.
    expect(showToastSpy).toHaveBeenCalledWith('gfs.pending_toast', 'info')
  })

  it('active: shows the connected copy + the publish CTA, which routes via loc.route', async () => {
    await connectGfs('active')

    expect(screen.getByText('gfs.connected')).toBeTruthy()
    expect(screen.queryByText('gfs.connected_pending')).toBeNull()
    expect(showToastSpy).toHaveBeenCalledWith('gfs.pair_success', 'success')

    // The "Open public sharing settings" CTA is present and navigates via
    // the base-aware client router — never location.assign to an absolute path.
    const cta = screen.getByText('gfs.open_publishing')
    fireEvent.click(cta)
    await new Promise(r => setTimeout(r, 0))
    expect(routeSpy).toHaveBeenCalledWith('/momentum/public/sharing')
  })
})
