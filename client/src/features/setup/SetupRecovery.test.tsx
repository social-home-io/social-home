/**
 * Tests for the "Restore from Recovery Kit" path in the first-boot
 * setup wizard. The restore entry point is a secondary affordance on
 * the welcome step (mode-agnostic — restore reconstitutes identity
 * regardless of deployment shape). On success the BACKEND RESTARTS and
 * returns no token, so the wizard shows a terminal "restored,
 * restarting — wait and reload" state rather than redirecting.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

beforeEach(() => {
  vi.resetModules()
})

function buildCfg(mode: 'standalone' | 'ha' | 'haos' = 'standalone') {
  return {
    value: {
      mode,
      instance_name: 'Home',
      capabilities: ['ingress', 'push', 'ai', 'ha_person_directory'],
      setup_required: true,
    },
  }
}

function commonMocks(cfg: ReturnType<typeof buildCfg>) {
  vi.doMock('@/store/instance', () => ({
    instanceConfig: cfg,
    loadInstanceConfig: vi.fn(async () => cfg.value),
  }))
  vi.doMock('@/store/auth', () => ({
    setToken: vi.fn(),
    loadCurrentUser: vi.fn(async () => null),
  }))
  vi.doMock('@/components/Toast', () => ({ showToast: vi.fn() }))
}

/** Stub FileReader so ``readAsDataURL`` resolves with a known data
 *  URI synchronously-ish (onload fires on the next microtask). */
function stubFileReader(dataUrl: string) {
  class FakeFileReader {
    public result: string | null = null
    public error: unknown = null
    public onload: (() => void) | null = null
    public onerror: (() => void) | null = null
    readAsDataURL() {
      this.result = dataUrl
      queueMicrotask(() => this.onload?.())
    }
  }
  vi.stubGlobal('FileReader', FakeFileReader as unknown as typeof FileReader)
}

function pickFile(container: ParentNode, file: File) {
  const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
  Object.defineProperty(fileInput, 'files', { value: [file], configurable: true })
  fireEvent.change(fileInput)
}

describe('SetupPage recovery restore', () => {
  it('reveals the restore form from the welcome step and can go back', async () => {
    vi.doMock('@/api', () => ({ api: { get: vi.fn(), post: vi.fn(), delete: vi.fn() } }))
    commonMocks(buildCfg('standalone'))
    const { SetupPage } = await import('./SetupPage')
    const { findByText, queryByText } = render(<SetupPage />)

    expect(await findByText('Welcome to your home')).toBeTruthy()
    // Restore form is not visible until the secondary CTA is clicked.
    expect(queryByText('Restore from a Recovery Kit')).toBeNull()

    fireEvent.click(await findByText(/Recovering a household/))
    expect(await findByText('Restore from a Recovery Kit')).toBeTruthy()

    // One-click way back to normal setup.
    fireEvent.click(await findByText('Back to normal setup'))
    expect(await findByText('Welcome to your home')).toBeTruthy()
  })

  it('shows an error and does NOT call the API when no file is chosen', async () => {
    const post = vi.fn()
    vi.doMock('@/api', () => ({ api: { get: vi.fn(), post, delete: vi.fn() } }))
    commonMocks(buildCfg('standalone'))
    const { SetupPage } = await import('./SetupPage')
    const { findByText, container } = render(<SetupPage />)

    fireEvent.click(await findByText(/Recovering a household/))
    // Passphrase only, no file.
    const pw = container.querySelector('input[type="password"]') as HTMLInputElement
    fireEvent.input(pw, { target: { value: 'hunter2' } })
    fireEvent.click(await findByText('Restore'))
    await new Promise((r) => setTimeout(r, 0))

    expect(await findByText('Choose a Recovery Kit file to restore.')).toBeTruthy()
    expect(post).not.toHaveBeenCalled()
  })

  it('shows an error and does NOT call the API when the passphrase is empty', async () => {
    const post = vi.fn()
    vi.doMock('@/api', () => ({ api: { get: vi.fn(), post, delete: vi.fn() } }))
    commonMocks(buildCfg('standalone'))
    stubFileReader('data:application/octet-stream;base64,QUJD')
    const { SetupPage } = await import('./SetupPage')
    const { findByText, container } = render(<SetupPage />)

    fireEvent.click(await findByText(/Recovering a household/))
    pickFile(container, new File(['ABC'], 'kit.shrk'))
    fireEvent.click(await findByText('Restore'))
    await new Promise((r) => setTimeout(r, 0))

    expect(await findByText('Enter the Kit passphrase.')).toBeTruthy()
    expect(post).not.toHaveBeenCalled()
  })

  it('POSTs {kit_b64, passphrase} and shows the terminal restart state on success', async () => {
    const post = vi.fn(async () => ({ instance_id: 'abc', restart_required: true }))
    vi.doMock('@/api', () => ({ api: { get: vi.fn(), post, delete: vi.fn() } }))
    commonMocks(buildCfg('standalone'))
    // Strip the data: prefix → bare base64 "QUJD".
    stubFileReader('data:application/octet-stream;base64,QUJD')
    const { SetupPage } = await import('./SetupPage')
    const { findByText, container } = render(<SetupPage />)

    fireEvent.click(await findByText(/Recovering a household/))
    pickFile(container, new File(['ABC'], 'kit.shrk'))
    const pw = container.querySelector('input[type="password"]') as HTMLInputElement
    fireEvent.input(pw, { target: { value: 'secret-pass' } })
    fireEvent.click(await findByText('Restore'))
    await new Promise((r) => setTimeout(r, 10))

    expect(post).toHaveBeenCalledWith('/api/setup/recovery/restore', {
      kit_b64: 'QUJD',
      passphrase: 'secret-pass',
    })
    // Terminal state: restart copy, NOT a redirect.
    expect(await findByText('Household restored')).toBeTruthy()
    expect(await findByText(/restarting now/)).toBeTruthy()
  })

  it('shows a friendly error on a 422 BAD_KIT', async () => {
    const err: any = new Error('bad')
    err.status = 422
    err.code = 'BAD_KIT'
    const post = vi.fn(async () => { throw err })
    vi.doMock('@/api', () => ({ api: { get: vi.fn(), post, delete: vi.fn() } }))
    commonMocks(buildCfg('standalone'))
    stubFileReader('data:application/octet-stream;base64,QUJD')
    const { SetupPage } = await import('./SetupPage')
    const { findByText, container } = render(<SetupPage />)

    fireEvent.click(await findByText(/Recovering a household/))
    pickFile(container, new File(['ABC'], 'kit.shrk'))
    const pw = container.querySelector('input[type="password"]') as HTMLInputElement
    fireEvent.input(pw, { target: { value: 'wrong-pass' } })
    fireEvent.click(await findByText('Restore'))
    await new Promise((r) => setTimeout(r, 10))

    expect(await findByText('Couldn’t open the kit — check the passphrase and file.')).toBeTruthy()
    expect(post).toHaveBeenCalled()
  })
})
