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
    mockPost: vi.fn().mockResolvedValue({ username: 'alice' }),
    currentUser: { value: null as Record<string, unknown> | null },
    FakeApiError,
  }
})

vi.mock('@/api', () => ({
  api: { post: mockPost },
  ApiError: FakeApiError,
}))

// Mock the auth store as a mutable holder so each test can vary ``source``
// and ``username`` and assert the local mirror gets updated. The holder is
// created in ``vi.hoisted`` above so the factory can reach it.
vi.mock('@/store/auth', () => ({ currentUser }))

import { UsernameEditor } from './UsernameEditor'

function setUser(over: Record<string, unknown> = {}) {
  currentUser.value = {
    user_id: 'u1',
    username: 'alice',
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
  mockPost.mockResolvedValue({ username: 'alice' })
  setUser()
})

describe('UsernameEditor — manual user', () => {
  it('renders an editable username field pre-filled with the current username', () => {
    setUser({ username: 'alice' })
    const { getByLabelText } = render(<UsernameEditor />)
    const input = getByLabelText(/username/i) as HTMLInputElement
    expect(input).toBeTruthy()
    expect(input.value).toBe('alice')
  })

  it('disables Save when the value is unchanged', () => {
    setUser({ username: 'alice' })
    const { getByText } = render(<UsernameEditor />)
    const btn = getByText('Save').closest('button') as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })

  it('disables Save when the value is empty (after trim)', () => {
    setUser({ username: 'alice' })
    const { getByLabelText, getByText } = render(<UsernameEditor />)
    const input = getByLabelText(/username/i) as HTMLInputElement
    fireEvent.input(input, { target: { value: '   ' } })
    const btn = getByText('Save').closest('button') as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })

  it('enables Save once the value changes to a non-empty trimmed value', () => {
    setUser({ username: 'alice' })
    const { getByLabelText, getByText } = render(<UsernameEditor />)
    const input = getByLabelText(/username/i) as HTMLInputElement
    fireEvent.input(input, { target: { value: 'bob' } })
    const btn = getByText('Save').closest('button') as HTMLButtonElement
    expect(btn.disabled).toBe(false)
  })

  it('posts me/username with the trimmed value on Save', async () => {
    setUser({ username: 'alice' })
    mockPost.mockResolvedValue({ username: 'bob' })
    const { getByLabelText, getByText } = render(<UsernameEditor />)
    const input = getByLabelText(/username/i) as HTMLInputElement
    fireEvent.input(input, { target: { value: '  bob  ' } })
    fireEvent.click(getByText('Save'))
    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/api/me/username', { username: 'bob' })
    })
  })

  it('updates the local currentUser username on success', async () => {
    setUser({ username: 'alice' })
    mockPost.mockResolvedValue({ username: 'bob' })
    const { getByLabelText, getByText } = render(<UsernameEditor />)
    fireEvent.input(getByLabelText(/username/i), { target: { value: 'bob' } })
    fireEvent.click(getByText('Save'))
    await waitFor(() => {
      expect(currentUser.value?.username).toBe('bob')
    })
  })

  it('shows the server message in a role=alert on a 422', async () => {
    setUser({ username: 'alice' })
    mockPost.mockRejectedValue(
      new FakeApiError(422, {
        code: 'INVALID_USERNAME',
        detail: 'That username is already taken.',
      }),
    )
    const { getByLabelText, getByText, findByRole } = render(<UsernameEditor />)
    fireEvent.input(getByLabelText(/username/i), { target: { value: 'taken' } })
    fireEvent.click(getByText('Save'))
    const alert = await findByRole('alert')
    expect(alert.textContent).toContain('already taken')
  })

  it('clears a previous error before the next save attempt', async () => {
    setUser({ username: 'alice' })
    mockPost.mockRejectedValueOnce(
      new FakeApiError(422, { code: 'INVALID_USERNAME', detail: 'Bad name.' }),
    )
    const { getByLabelText, getByText, findByRole, queryByRole } = render(
      <UsernameEditor />,
    )
    fireEvent.input(getByLabelText(/username/i), { target: { value: 'x!' } })
    fireEvent.click(getByText('Save'))
    await findByRole('alert')
    // Second attempt succeeds — the alert should clear.
    mockPost.mockResolvedValue({ username: 'okname' })
    fireEvent.input(getByLabelText(/username/i), { target: { value: 'okname' } })
    fireEvent.click(getByText('Save'))
    await waitFor(() => {
      expect(queryByRole('alert')).toBeNull()
    })
  })
})

describe('UsernameEditor — HA-sourced user', () => {
  it('shows the username read-only with a managed-by-HA note, no input, no Save', () => {
    setUser({ username: 'ha_alice', source: 'ha' })
    const { queryByLabelText, queryByText, getByText } = render(
      <UsernameEditor />,
    )
    // No editable field, no Save button.
    expect(queryByLabelText(/username/i)).toBeNull()
    expect(queryByText('Save')).toBeNull()
    // Read-only username + the managed note are both present.
    expect(getByText('ha_alice')).toBeTruthy()
    expect(getByText(/managed by Home Assistant/i)).toBeTruthy()
  })

  it('never posts for an HA-sourced user', () => {
    setUser({ username: 'ha_alice', source: 'ha' })
    render(<UsernameEditor />)
    expect(mockPost).not.toHaveBeenCalled()
  })
})
