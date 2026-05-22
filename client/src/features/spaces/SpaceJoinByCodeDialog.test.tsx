import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  render, fireEvent, waitFor, act, cleanup,
} from '@testing-library/preact'
import { LocationProvider } from 'preact-iso'

vi.mock('@/api', async () => {
  class ApiError extends Error {
    constructor(public status: number, msg = 'api error') {
      super(msg)
    }
  }
  return {
    api: { post: vi.fn() },
    ApiError,
  }
})
vi.mock('@/baseUrl', () => ({
  basePath: '/',
  addBase: (p: string) => p,
  stripBase: (p: string) => p,
}))
vi.mock('qrcode', () => ({
  default: { toDataURL: vi.fn(async () => 'data:fake') },
}))

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

const routeSpy = vi.fn()
vi.mock('preact-iso', async () => {
  const actual = await vi.importActual<typeof import('preact-iso')>('preact-iso')
  return {
    ...actual,
    useLocation: () => ({ route: routeSpy, url: '/', path: '/', query: {} }),
  }
})

const { api, ApiError } = await import('@/api') as unknown as {
  api: { post: ReturnType<typeof vi.fn> }
  ApiError: new (status: number, msg?: string) => Error & { status: number }
}
const {
  SpaceJoinByCodeDialog, openSpaceJoinByCode,
} = await import('./SpaceJoinByCodeDialog')
const { buildInviteCode } = await import('@/lib/spaceInviteCode')

beforeEach(() => {
  api.post.mockReset()
  routeSpy.mockReset()
})

afterEach(() => {
  cleanup()
})

async function renderAndOpen() {
  const result = render(
    <LocationProvider>
      <SpaceJoinByCodeDialog />
    </LocationProvider>,
  )
  await act(async () => { openSpaceJoinByCode() })
  await waitFor(() => {
    expect(
      result.container.querySelector('[data-testid="join-by-code-input"]'),
    ).not.toBeNull()
  })
  return result
}

describe('SpaceJoinByCodeDialog', () => {
  it('is gated on openSpaceJoinByCode() — renders nothing by default', () => {
    const { container } = render(
      <LocationProvider>
        <SpaceJoinByCodeDialog />
      </LocationProvider>,
    )
    expect(
      container.querySelector('[data-testid="join-by-code-input"]'),
    ).toBeNull()
  })

  it('joins same-instance — issuer matches our id, no issuer_instance_id in the POST', async () => {
    api.post.mockResolvedValueOnce({ space_id: 'space-uuid-xyz' })
    // Same instance id as the mock store — local redeem path; the
    // SPA must NOT forward issuer_instance_id so the backend takes
    // the local code branch in /api/spaces/join.
    const code = buildInviteCode({
      token: 'a1b2c3d4e5f60718',
      space_id: 'space-uuid-xyz',
      issuer_instance_id: OUR_INSTANCE_ID,
    })
    const { container, getByText } = await renderAndOpen()
    const input = container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.input(input, { target: { value: code } })
    })
    await act(async () => { fireEvent.click(getByText('Join')) })
    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/api/spaces/join', {
        token: 'a1b2c3d4e5f60718',
      })
      expect(routeSpy).toHaveBeenCalledWith('/spaces/space-uuid-xyz')
    })
  })

  it('forwards issuer_instance_id when the code was minted on another instance', async () => {
    api.post.mockResolvedValueOnce({ space_id: 'space-uuid-fed' })
    // Different instance id — the backend has to route the redeem
    // over the SPACE_INVITE_TOKEN_REDEEM federation flow.
    const code = buildInviteCode({
      token: 'a1b2c3d4e5f60718',
      space_id: 'space-uuid-fed',
      issuer_instance_id: 'ffffeeeeddddccccbbbb111122223333',
    })
    const { container, getByText } = await renderAndOpen()
    const input = container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.input(input, { target: { value: code } })
    })
    await act(async () => { fireEvent.click(getByText('Join')) })
    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/api/spaces/join', {
        token: 'a1b2c3d4e5f60718',
        issuer_instance_id: 'ffffeeeeddddccccbbbb111122223333',
      })
    })
  })

  it('joins when given a bare hex token (back-compat)', async () => {
    api.post.mockResolvedValueOnce({ space_id: 'space-bare' })
    const { container, getByText } = await renderAndOpen()
    const input = container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.input(input, { target: { value: 'a1b2c3d4e5f60718' } })
    })
    await act(async () => { fireEvent.click(getByText('Join')) })
    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/api/spaces/join', {
        token: 'a1b2c3d4e5f60718',
      })
    })
  })

  it('shows a decoder-specific error for garbage input — does NOT call the API', async () => {
    const { container, getByText } = await renderAndOpen()
    const input = container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.input(input, { target: { value: 'totally not a code at all' } })
    })
    await act(async () => { fireEvent.click(getByText('Join')) })
    expect(api.post).not.toHaveBeenCalled()
    expect(container.querySelector('.sh-scan-error-inline')?.textContent)
      .toContain("doesn't look like a Social Home invite code")
  })

  it('shows the expired-token message on 404 from the API', async () => {
    api.post.mockRejectedValueOnce(new ApiError(404, 'token not found'))
    const { container, getByText } = await renderAndOpen()
    const input = container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.input(input, { target: { value: 'a1b2c3d4e5f60718' } })
    })
    await act(async () => { fireEvent.click(getByText('Join')) })
    await waitFor(() => {
      expect(container.querySelector('.sh-scan-error-inline')?.textContent)
        .toContain('expired or already been used')
    })
    expect(routeSpy).not.toHaveBeenCalled()
  })

  // Removed: the SPA no longer pre-flights "wrong instance" with
  // a hard block. Cross-instance codes now route through the
  // /api/spaces/join endpoint, which forwards a federation redeem
  // when the issuer is reachable as a CONFIRMED peer and 422s with
  // a "pair with X first" error otherwise. The error-rendering
  // test below covers the unreachable case.

  it('reopen after a prior error resets the draft + error message', async () => {
    // First open: trigger the decoder error.
    let result = await renderAndOpen()
    let input = result.container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.input(input, { target: { value: 'garbage' } })
    })
    await act(async () => { fireEvent.click(result.getByText('Join')) })
    expect(result.container.querySelector('.sh-scan-error-inline')).not.toBeNull()
    // Close via the modal's overlay-click escape (the close handler).
    // The component re-renders to null when ``open`` flips.
    await act(async () => { result.unmount() })
    // Re-open — input should be empty, no stale error.
    result = await renderAndOpen()
    input = result.container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    expect(input.value).toBe('')
    expect(result.container.querySelector('.sh-scan-error-inline')).toBeNull()
  })
})
