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

// The dialog reads our own instance id from the SPA's
// :class:`instanceConfig` store and stamps it into the code.
const OUR_INSTANCE_ID = 'aaaabbbbccccddddeeeeffff00001111'
vi.mock('@/store/instance', () => ({
  instanceConfig: {
    value: {
      mode: 'standalone',
      instance_name: 'Test home',
      instance_id: OUR_INSTANCE_ID,
      capabilities: [],
      setup_required: false,
    },
  },
  loadInstanceConfig: vi.fn(),
}))

const { api } = await import('@/api') as unknown as {
  api: { get: ReturnType<typeof vi.fn>; post: ReturnType<typeof vi.fn> }
}
const { openSpaceInvite, SpaceInviteDialog } = await import('./SpaceInviteDialog')

beforeEach(() => {
  api.get.mockReset()
  api.post.mockReset()
})

afterEach(() => {
  cleanup()
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
  it('does NOT render an HTTPS link artifact', async () => {
    // The clickable-link path can't redeem cross-instance — the
    // receiver has to be on their own instance to call the redeem
    // RPC. Until a GFS-mediated redirect exists, the code paste is
    // the only path we offer.
    const { container } = await openAndGenerate({
      hint: 'Pascal\'s family',
      returnedToken: 'tok-aaaa-bbbb-cccc',
    })
    expect(container.querySelector('[data-testid="invite-link"]')).toBeNull()
  })

  it('emits a socialhome://invite#... code that decodes back to the token + issuer instance id', async () => {
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
    // The new wire shape — issuer's stable id, not a URL — so the
    // receiver's instance can route the redeem over federation.
    expect(decoded.issuer_instance_id).toBe(OUR_INSTANCE_ID)
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
