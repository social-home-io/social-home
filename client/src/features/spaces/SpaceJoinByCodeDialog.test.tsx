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

  it('joins when given a valid socialhome://invite#... code', async () => {
    api.post.mockResolvedValueOnce({ space_id: 'space-uuid-xyz' })
    // Match document.baseURI exactly so isWrongInstance() returns false.
    const code = buildInviteCode({
      token: 'a1b2c3d4e5f60718',
      space_id: 'space-uuid-xyz',
      issuer_instance_url: document.baseURI,
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
    expect(container.querySelector('.sh-error')?.textContent)
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
      expect(container.querySelector('.sh-error')?.textContent)
        .toContain('expired or already been used')
    })
    expect(routeSpy).not.toHaveBeenCalled()
  })

  it('shows the wrong-instance message before calling the API when issuer URLs disagree', async () => {
    const code = buildInviteCode({
      token: 'a1b2c3d4e5f60718',
      space_id: 'space-x',
      issuer_instance_url: 'http://some-other-host/',
    })
    const { container, getByText } = await renderAndOpen()
    const input = container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.input(input, { target: { value: code } })
    })
    await act(async () => { fireEvent.click(getByText('Join')) })
    expect(api.post).not.toHaveBeenCalled()
    expect(container.querySelector('.sh-error')?.textContent)
      .toContain('another Social Home instance')
  })

  it('reopen after a prior error resets the draft + error message', async () => {
    // First open: trigger the decoder error.
    let result = await renderAndOpen()
    let input = result.container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.input(input, { target: { value: 'garbage' } })
    })
    await act(async () => { fireEvent.click(result.getByText('Join')) })
    expect(result.container.querySelector('.sh-error')).not.toBeNull()
    // Close via the modal's overlay-click escape (the close handler).
    // The component re-renders to null when ``open`` flips.
    await act(async () => { result.unmount() })
    // Re-open — input should be empty, no stale error.
    result = await renderAndOpen()
    input = result.container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    expect(input.value).toBe('')
    expect(result.container.querySelector('.sh-error')).toBeNull()
  })
})
