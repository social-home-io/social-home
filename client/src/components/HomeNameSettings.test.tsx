import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

// Mock the API module before importing the component. ``vi.hoisted`` is
// the only way to define a ``vi.fn`` reachable from a ``vi.mock`` factory
// — vitest hoists the factory above plain ``const`` declarations.
const { mockPatch } = vi.hoisted(() => ({
  mockPatch: vi.fn().mockResolvedValue({ display_name: 'Casa Vizeli' }),
}))

vi.mock('@/api', () => ({
  api: {
    get: vi.fn().mockResolvedValue({}),
    post: vi.fn().mockResolvedValue({}),
    patch: mockPatch,
    delete: vi.fn().mockResolvedValue(undefined),
    upload: vi.fn().mockResolvedValue({}),
  },
}))

vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { instanceConfig } from '@/store/instance'

function setName(name: string) {
  instanceConfig.value = {
    mode: 'standalone',
    instance_name: name,
    instance_id: 'i1',
    capabilities: [],
    setup_required: false,
  }
}

beforeEach(() => {
  setName('Home')
  mockPatch.mockReset()
  mockPatch.mockResolvedValue({ display_name: 'Casa Vizeli' })
})

describe('HomeNameSettings', () => {
  it('renders an input pre-filled with the current instance_name', async () => {
    setName('Vizeli Manor')
    const { HomeNameSettings } = await import('./HomeNameSettings')
    const { container } = render(<HomeNameSettings />)
    const input = container.querySelector('input[type="text"]') as HTMLInputElement
    expect(input).toBeTruthy()
    expect(input.value).toBe('Vizeli Manor')
  })

  it('editing + Save PATCHes /api/admin/instance and updates the store', async () => {
    const { HomeNameSettings } = await import('./HomeNameSettings')
    const { container, getByText } = render(<HomeNameSettings />)
    const input = container.querySelector('input[type="text"]') as HTMLInputElement
    fireEvent.input(input, { target: { value: 'Casa Vizeli' } })
    fireEvent.click(getByText('Save'))
    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith('/api/admin/instance', {
        display_name: 'Casa Vizeli',
      })
    })
    await waitFor(() => {
      expect(instanceConfig.value?.instance_name).toBe('Casa Vizeli')
    })
  })

  it('does not PATCH when the trimmed name is empty', async () => {
    const { HomeNameSettings } = await import('./HomeNameSettings')
    const { container, getByText } = render(<HomeNameSettings />)
    const input = container.querySelector('input[type="text"]') as HTMLInputElement
    fireEvent.input(input, { target: { value: '   ' } })
    fireEvent.click(getByText('Save'))
    expect(mockPatch).not.toHaveBeenCalled()
  })

  it('does not PATCH when the name is unchanged', async () => {
    setName('Home')
    const { HomeNameSettings } = await import('./HomeNameSettings')
    const { getByText } = render(<HomeNameSettings />)
    fireEvent.click(getByText('Save'))
    expect(mockPatch).not.toHaveBeenCalled()
  })
})
