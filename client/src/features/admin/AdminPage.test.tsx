import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

// Mock the API module before importing the page
vi.mock('@/api', () => ({
  api: {
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))

// Mock auth store
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

// Toast is a side-effect surface; silence it so assertions focus on the
// FormError / fetch contract.
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

describe('AdminPage', () => {
  it('module exports a default component', async () => {
    const mod = await import('./AdminPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  })
})

describe('RecoveryKitPanel', () => {
  let createObjectURL: ReturnType<typeof vi.fn>
  let revokeObjectURL: ReturnType<typeof vi.fn>
  let anchorClick: ReturnType<typeof vi.fn<() => void>>

  beforeEach(() => {
    localStorage.setItem('sh-token', 'test-tok')
    createObjectURL = vi.fn(() => 'blob:fake')
    revokeObjectURL = vi.fn()
    // jsdom has no createObjectURL — stub the pair the download flow needs.
    URL.createObjectURL = createObjectURL as unknown as typeof URL.createObjectURL
    URL.revokeObjectURL = revokeObjectURL as unknown as typeof URL.revokeObjectURL
    anchorClick = vi.fn<() => void>()
    // Intercept the temporary <a download> click so jsdom doesn't try to
    // navigate; everything else (href/download) stays a real element.
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(anchorClick)
    vi.stubGlobal('fetch', vi.fn())
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    localStorage.clear()
  })

  async function renderRecoveryKit() {
    const mod = await import('./AdminPage')
    const { RecoveryKitPanel } = mod
    return render(<RecoveryKitPanel />)
  }

  it('shows an error and does NOT call the API when the passphrase is too short', async () => {
    const { getByLabelText, getByText, findByRole } = await renderRecoveryKit()
    fireEvent.input(getByLabelText(/^Passphrase$/i), { target: { value: 'short' } })
    fireEvent.input(getByLabelText(/Confirm passphrase/i), { target: { value: 'short' } })
    fireEvent.click(getByText(/Generate & download/i))

    const alert = await findByRole('alert')
    expect(alert).toBeTruthy()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('shows an error and does NOT call the API when the passphrases differ', async () => {
    const { getByLabelText, getByText, findByRole } = await renderRecoveryKit()
    fireEvent.input(getByLabelText(/^Passphrase$/i), { target: { value: 'longenough1' } })
    fireEvent.input(getByLabelText(/Confirm passphrase/i), { target: { value: 'longenough2' } })
    fireEvent.click(getByText(/Generate & download/i))

    const alert = await findByRole('alert')
    expect(alert).toBeTruthy()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('POSTs the passphrase and triggers a .shrk blob download on success', async () => {
    const blob = new Blob(['sealed'], { type: 'application/octet-stream' })
    ;(fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      status: 200,
      blob: () => Promise.resolve(blob),
    })

    const { getByLabelText, getByText } = await renderRecoveryKit()
    const pass = getByLabelText(/^Passphrase$/i) as HTMLInputElement
    fireEvent.input(pass, { target: { value: 'correct-horse' } })
    fireEvent.input(getByLabelText(/Confirm passphrase/i), { target: { value: 'correct-horse' } })
    fireEvent.click(getByText(/Generate & download/i))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(url).toBe('api/recovery-kit')
    expect(init.method).toBe('POST')
    expect(init.headers.Authorization).toBe('Bearer test-tok')
    expect(JSON.parse(init.body)).toEqual({ passphrase: 'correct-horse' })

    await waitFor(() => expect(anchorClick).toHaveBeenCalled())
    expect(createObjectURL).toHaveBeenCalledWith(blob)
    // Fields are cleared after a successful download.
    await waitFor(() => expect(pass.value).toBe(''))
  })

  it('shows an error when the server rejects the request', async () => {
    ;(fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      status: 422,
      blob: () => Promise.resolve(new Blob()),
    })

    const { getByLabelText, getByText, findByRole } = await renderRecoveryKit()
    fireEvent.input(getByLabelText(/^Passphrase$/i), { target: { value: 'correct-horse' } })
    fireEvent.input(getByLabelText(/Confirm passphrase/i), { target: { value: 'correct-horse' } })
    fireEvent.click(getByText(/Generate & download/i))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    const alert = await findByRole('alert')
    expect(alert).toBeTruthy()
    expect(anchorClick).not.toHaveBeenCalled()
  })
})
