import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  render, fireEvent, waitFor, act, cleanup,
} from '@testing-library/preact'
import { decodeInviteCode } from '@/lib/spaceInviteCode'

vi.mock('@/api', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))
vi.mock('qrcode', () => ({
  default: { toDataURL: vi.fn(async (data: string) => `data:fake;${data}`) },
}))
// Force ``basePath`` / ``addBase`` to the ingress shape — the bug
// Slice B is fixing is that ``location.origin`` skips this prefix.
const INGRESS_PREFIX = '/api/hassio_ingress/abc'
vi.mock('@/baseUrl', () => ({
  basePath: `${INGRESS_PREFIX}/`,
  addBase: (path: string) => {
    const [pathOnly, ...rest] = path.split(/(?=[?#])/, 2)
    const suffix = rest.join('')
    const normalised = pathOnly.startsWith('/') ? pathOnly : '/' + pathOnly
    return INGRESS_PREFIX + normalised + suffix
  },
  stripBase: (p: string) => p,
}))

const { api } = await import('@/api') as unknown as {
  api: { get: ReturnType<typeof vi.fn>; post: ReturnType<typeof vi.fn> }
}
const { openSpaceInvite, SpaceInviteDialog } = await import('./SpaceInviteDialog')

const INGRESS_BASE = `http://homeassistant.local${INGRESS_PREFIX}/`
const originalBaseURI = Object.getOwnPropertyDescriptor(
  Document.prototype, 'baseURI',
)

beforeEach(() => {
  Object.defineProperty(document, 'baseURI', {
    configurable: true,
    get: () => INGRESS_BASE,
  })
  api.get.mockReset()
  api.post.mockReset()
})

afterEach(() => {
  cleanup()
  if (originalBaseURI) {
    Object.defineProperty(Document.prototype, 'baseURI', originalBaseURI)
  }
})

async function openAndGenerate(
  { hint, returnedToken }: { hint: string | null; returnedToken: string },
) {
  api.post.mockResolvedValueOnce({ token: returnedToken })
  if (hint === null) api.get.mockResolvedValueOnce({ name: 'Fetched space name' })
  const result = render(<SpaceInviteDialog />)
  await act(async () => {
    openSpaceInvite('space-uuid-' + Math.random().toString(36).slice(2, 10), hint)
  })
  await waitFor(() => {
    expect(result.container.querySelector('.sh-invite-dialog'))
      .not.toBeNull()
  })
  const generateBtn = result.getByText('Generate invite')
  await act(async () => { fireEvent.click(generateBtn) })
  await waitFor(() => {
    expect(result.container.querySelector('[data-testid="invite-code"]'))
      .not.toBeNull()
  })
  return result
}

describe('SpaceInviteDialog', () => {
  it('does NOT render an HTTPS link — receiver has to be on their own instance', async () => {
    // The dialog used to surface a "Link · clickable from email"
    // artifact, but plain HTTPS links can't redeem the token (the
    // receiver has to be on their own instance to call
    // /api/spaces/join). Until a GFS-mediated redirect lifts that
    // limitation, the code + QR are the only artifacts that work.
    const { container } = await openAndGenerate({
      hint: 'Pascal\'s family',
      returnedToken: 'tok-aaaa-bbbb-cccc',
    })
    expect(container.querySelector('[data-testid="invite-link"]')).toBeNull()
  })

  it('emits a socialhome://invite#... code that decodes back to the token + metadata', async () => {
    const { container } = await openAndGenerate({
      hint: 'Pascal\'s family',
      returnedToken: 'tok-xxxx-yyyy-zzzz',
    })
    const code = container.querySelector('[data-testid="invite-code"]')!
      .textContent!
    expect(code.startsWith('socialhome://invite#')).toBe(true)
    const decoded = decodeInviteCode(code)!
    expect(decoded.token).toBe('tok-xxxx-yyyy-zzzz')
    expect(decoded.space_display_hint).toBe('Pascal\'s family')
    expect(decoded.issuer_instance_url).toBe(INGRESS_BASE)
  })

  it('fetches the space name when the caller does not supply a hint', async () => {
    const { container } = await openAndGenerate({
      hint: null,
      returnedToken: 'tok-aaaa',
    })
    expect(api.get).toHaveBeenCalled()
    const code = container.querySelector('[data-testid="invite-code"]')!
      .textContent!
    const decoded = decodeInviteCode(code)!
    expect(decoded.space_display_hint).toBe('Fetched space name')
  })

  it('"Make another" wipes the artifacts and returns to the generate state', async () => {
    const { container, getByText } = await openAndGenerate({
      hint: 'X',
      returnedToken: 'tok-1',
    })
    expect(container.querySelector('[data-testid="invite-code"]')).not.toBeNull()
    await act(async () => { fireEvent.click(getByText('Make another')) })
    await waitFor(() => {
      expect(container.querySelector('[data-testid="invite-code"]')).toBeNull()
    })
    expect(getByText('Generate invite')).toBeTruthy()
  })

  it('renders a QR placeholder or img for the invite code', async () => {
    const { container } = await openAndGenerate({
      hint: 'X', returnedToken: 'tok-q',
    })
    const qr = container.querySelector('.sh-qr-skeleton, .sh-qr-code')
    expect(qr).not.toBeNull()
  })
})
