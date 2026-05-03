import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

vi.mock('@/i18n/i18n', () => ({
  t: (key: string) => key,
  locale: { value: 'en' },
  setLocale: vi.fn(),
}))
vi.mock('@/api', () => {
  const m = { get: vi.fn(), put: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
  return { api: m }
})
vi.mock('./Toast', () => ({ showToast: vi.fn() }))

import { SpaceSettings } from './SpaceSettings'
import { api } from '@/api'

const apiMock = api as unknown as {
  get: ReturnType<typeof vi.fn>
  patch: ReturnType<typeof vi.fn>
  delete: ReturnType<typeof vi.fn>
}

function makeSpace(overrides: Partial<{
  retention_days: number | null
  features: object
}> = {}) {
  return {
    id: 's-1',
    name: 'Trip group',
    description: '',
    emoji: null,
    space_type: 'private' as const,
    join_mode: 'invite_only' as const,
    features: overrides.features ?? {
      calendar: true, todo: true, location: false,
      stickies: false, pages: true,
      posts_access: 'open', pages_access: 'open',
      stickies_access: 'open', calendar_access: 'open',
      tasks_access: 'open',
      allowed_post_types: ['text'],
    },
    retention_days: overrides.retention_days ?? null,
  } as never
}

describe('SpaceSettings', () => {
  beforeEach(() => {
    apiMock.get.mockResolvedValue([])
    apiMock.patch.mockReset()
  })

  it('module exports exist', async () => {
    const mod = await import('./SpaceSettings')
    expect(mod).toBeTruthy()
    expect(typeof mod.SpaceSettings).toBe('function')
  })

  it('renders the retention input prefilled with the space value', () => {
    const space = makeSpace({ retention_days: 30 })
    const { container } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const input = container.querySelector(
      'input[type="number"]',
    ) as HTMLInputElement | null
    expect(input).toBeTruthy()
    expect(input!.value).toBe('30')
  })

  it('sends retention_days in the PATCH on save', async () => {
    apiMock.patch.mockResolvedValueOnce({})
    const space = makeSpace({ retention_days: null })
    const { container, getByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const input = container.querySelector(
      'input[type="number"]',
    ) as HTMLInputElement
    fireEvent.input(input, { target: { value: '90' } })
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    expect(apiMock.patch).toHaveBeenCalledOnce()
    const [, body] = apiMock.patch.mock.calls[0]
    expect(body.retention_days).toBe(90)
  })

  it('sends 0 for retention_days when the field is cleared (= forever)', async () => {
    apiMock.patch.mockResolvedValueOnce({})
    const space = makeSpace({ retention_days: 90 })
    const { container, getByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const input = container.querySelector(
      'input[type="number"]',
    ) as HTMLInputElement
    fireEvent.input(input, { target: { value: '' } })
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    const [, body] = apiMock.patch.mock.calls[0]
    expect(body.retention_days).toBe(0)
  })

  it('preserves typed name across re-renders triggered by sibling state', async () => {
    // Regression for the signal-in-render footgun: previously the
    // form rebuilt fresh ``signal()`` instances on every render, so
    // typing into ``name`` and then triggering a render via a sibling
    // change (e.g. toggling location-sharing) silently dropped the
    // typed value back to the prop default. ``useSignal`` keeps the
    // instance stable; this test guards that invariant.
    apiMock.patch.mockResolvedValueOnce({})
    const space = makeSpace()
    const { container, getByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    // Name input is the first ``<input>`` in the form (no ``type``
    // attribute — defaults to ``text``).
    const nameInput = container.querySelector(
      '.sh-form input',
    ) as HTMLInputElement
    expect(nameInput).toBeTruthy()
    fireEvent.input(nameInput, { target: { value: 'New name' } })
    // Trigger a re-render by toggling the location checkbox.
    const checkbox = container.querySelector(
      'input[type="checkbox"]',
    ) as HTMLInputElement
    fireEvent.change(checkbox, { target: { checked: true } })
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    const [, body] = apiMock.patch.mock.calls[0]
    expect(body.name).toBe('New name')
  })
})
