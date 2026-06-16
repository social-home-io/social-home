import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

// Mock the API module before importing the component. ``vi.hoisted`` is the
// only way to define a ``vi.fn`` reachable from the ``vi.mock`` factory
// (vitest hoists the factory above plain ``const`` declarations).
const { mockPost, currentUser, FakeApiError } = vi.hoisted(() => {
  // Real-ish ApiError stand-in carrying ``status`` + ``code`` + ``detail`` so
  // the component can branch on a 422 the same way it does against the live
  // ApiError (whose ``message`` defaults to ``detail``). Built inside
  // ``vi.hoisted`` so the ``vi.mock('@/api')`` factory can reach it.
  class FakeApiError extends Error {
    status: number
    code: string | null
    detail: string | null
    constructor(
      status: number,
      parsed?: { code?: string; detail?: string } | null,
    ) {
      super(parsed?.detail ?? `API ${status}`)
      this.name = 'ApiError'
      this.status = status
      this.code = parsed?.code ?? null
      this.detail = parsed?.detail ?? null
    }
  }
  return {
    mockPost: vi.fn().mockResolvedValue({ handle: 'alice' }),
    currentUser: { value: null as Record<string, unknown> | null },
    FakeApiError,
  }
})

vi.mock('@/api', () => ({
  api: { post: mockPost },
  ApiError: FakeApiError,
}))

// Mock the auth store as a mutable holder so each test can vary ``handle``
// and assert the local mirror gets updated. The holder is created in
// ``vi.hoisted`` above so the factory can reach it.
vi.mock('@/store/auth', () => ({ currentUser }))

import { HandleEditor } from './HandleEditor'

function setUser(over: Record<string, unknown> = {}) {
  currentUser.value = {
    user_id: 'u1',
    username: 'alice',
    handle: 'alice',
    display_name: 'Alice',
    source: 'manual',
    is_admin: false,
    picture_url: null,
    bio: null,
    is_new_member: false,
    ...over,
  }
}

beforeEach(() => {
  mockPost.mockReset()
  mockPost.mockResolvedValue({ handle: 'alice' })
  setUser()
})

describe('HandleEditor', () => {
  it('renders an editable handle field pre-filled with the current handle', () => {
    setUser({ handle: 'alice' })
    const { getByLabelText } = render(<HandleEditor />)
    const input = getByLabelText(/handle/i) as HTMLInputElement
    expect(input).toBeTruthy()
    expect(input.value).toBe('alice')
  })

  it('pre-fills the username when the handle is still null, with Save disabled until changed', () => {
    // Legacy/edge rows can have a null handle (provisioned before the
    // handle-seeding fix). The editor falls back to the username so the field
    // is never empty — the user sees their @-name and can save it. Save stays
    // disabled until they actually change it (consistent "unchanged" logic).
    setUser({ username: 'alice', handle: null })
    const { getByLabelText, getByText } = render(<HandleEditor />)
    const input = getByLabelText(/handle/i) as HTMLInputElement
    expect(input.value).toBe('alice')
    const btn = getByText('Save').closest('button') as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    fireEvent.input(input, { target: { value: 'alice2' } })
    expect(btn.disabled).toBe(false)
  })

  it('disables Save when the value is unchanged', () => {
    setUser({ handle: 'alice' })
    const { getByText } = render(<HandleEditor />)
    const btn = getByText('Save').closest('button') as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })

  it('disables Save when the value is empty (after trim)', () => {
    setUser({ handle: 'alice' })
    const { getByLabelText, getByText } = render(<HandleEditor />)
    const input = getByLabelText(/handle/i) as HTMLInputElement
    fireEvent.input(input, { target: { value: '   ' } })
    const btn = getByText('Save').closest('button') as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })

  it('enables Save once the value changes to a non-empty trimmed value', () => {
    setUser({ handle: 'alice' })
    const { getByLabelText, getByText } = render(<HandleEditor />)
    const input = getByLabelText(/handle/i) as HTMLInputElement
    fireEvent.input(input, { target: { value: 'bob' } })
    const btn = getByText('Save').closest('button') as HTMLButtonElement
    expect(btn.disabled).toBe(false)
  })

  it('posts /api/me/handle with the trimmed value on Save', async () => {
    setUser({ handle: 'alice' })
    mockPost.mockResolvedValue({ handle: 'bob' })
    const { getByLabelText, getByText } = render(<HandleEditor />)
    const input = getByLabelText(/handle/i) as HTMLInputElement
    fireEvent.input(input, { target: { value: '  bob  ' } })
    fireEvent.click(getByText('Save'))
    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/api/me/handle', { handle: 'bob' })
    })
  })

  it('updates the local currentUser handle on success', async () => {
    setUser({ handle: 'alice' })
    mockPost.mockResolvedValue({ handle: 'bob' })
    const { getByLabelText, getByText } = render(<HandleEditor />)
    fireEvent.input(getByLabelText(/handle/i), { target: { value: 'bob' } })
    fireEvent.click(getByText('Save'))
    await waitFor(() => {
      expect(currentUser.value?.handle).toBe('bob')
    })
  })

  it('shows the server message in a role=alert on a 422', async () => {
    setUser({ handle: 'alice' })
    mockPost.mockRejectedValue(
      new FakeApiError(422, {
        code: 'INVALID_HANDLE',
        detail: 'That handle is already taken.',
      }),
    )
    const { getByLabelText, getByText, findByRole } = render(<HandleEditor />)
    fireEvent.input(getByLabelText(/handle/i), { target: { value: 'taken' } })
    fireEvent.click(getByText('Save'))
    const alert = await findByRole('alert')
    expect(alert.textContent).toContain('already taken')
  })

  it('clears a previous error before the next save attempt', async () => {
    setUser({ handle: 'alice' })
    mockPost.mockRejectedValueOnce(
      new FakeApiError(422, { code: 'INVALID_HANDLE', detail: 'Bad handle.' }),
    )
    const { getByLabelText, getByText, findByRole, queryByRole } = render(
      <HandleEditor />,
    )
    fireEvent.input(getByLabelText(/handle/i), { target: { value: 'x!' } })
    fireEvent.click(getByText('Save'))
    await findByRole('alert')
    // Second attempt succeeds — the alert should clear.
    mockPost.mockResolvedValue({ handle: 'okhandle' })
    fireEvent.input(getByLabelText(/handle/i), { target: { value: 'okhandle' } })
    fireEvent.click(getByText('Save'))
    await waitFor(() => {
      expect(queryByRole('alert')).toBeNull()
    })
  })

  it('is editable for an HA-source user (no read-only gate, unlike username)', async () => {
    // The handle is editable by ALL users — an HA-provisioned account gets the
    // same editable field + Save action as a manual one.
    setUser({ username: 'ha_alice', handle: 'ha_alice', source: 'ha' })
    mockPost.mockResolvedValue({ handle: 'newhandle' })
    const { getByLabelText, getByText } = render(<HandleEditor />)
    const input = getByLabelText(/handle/i) as HTMLInputElement
    expect(input).toBeTruthy()
    fireEvent.input(input, { target: { value: 'newhandle' } })
    fireEvent.click(getByText('Save'))
    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/api/me/handle', {
        handle: 'newhandle',
      })
    })
  })
})
