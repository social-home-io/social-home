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

vi.mock('@/ws', () => ({
  ws: { on: vi.fn(() => () => {}) },
}))

vi.mock('@/i18n/i18n', () => ({
  t: (key: string) => key,
  locale: { value: 'en' },
  setLocale: vi.fn(),
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
  writeText.mockReset().mockResolvedValue(undefined)
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
