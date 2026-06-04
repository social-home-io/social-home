import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

// Mock the API module before importing the component. ``get`` returns a
// valid household theme so ``load()`` resolves and the form renders.
vi.mock('@/api', () => ({
  api: {
    get: vi.fn().mockResolvedValue({
      primary_color: '#D2542A',
      accent_color: '#C8902F',
      surface_color: null,
      surface_dark: null,
      mode: 'auto',
      font_family: 'system',
      density: 'comfortable',
      corner_radius: 12,
    }),
    put: vi.fn().mockResolvedValue({}),
  },
}))

// Toast is a side effect we don't need to assert on here.
vi.mock('./Toast', () => ({ showToast: vi.fn() }))

import { api } from '@/api'

describe('HouseholdThemeStudio', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('module exports exist', async () => {
    const mod = await import('./HouseholdThemeStudio')
    expect(mod.HouseholdThemeStudio).toBeTruthy()
    expect(typeof mod.HouseholdThemeStudio).toBe('function')
  })

  it('does not load the household name from /api/household/features', async () => {
    const mod = await import('./HouseholdThemeStudio')
    const { HouseholdThemeStudio } = mod
    render(<HouseholdThemeStudio />)
    await new Promise((r) => setTimeout(r, 0))
    // The studio only reads the theme — the name lives in admin Settings.
    expect(api.get).toHaveBeenCalledWith('/api/theme')
    expect(api.get).not.toHaveBeenCalledWith('/api/household/features')
  })

  it('saves the theme via PUT /api/theme but never PUTs /api/household/features', async () => {
    const mod = await import('./HouseholdThemeStudio')
    const { HouseholdThemeStudio } = mod
    const { getByText } = render(<HouseholdThemeStudio />)
    await new Promise((r) => setTimeout(r, 0))

    fireEvent.click(getByText('Save'))
    await new Promise((r) => setTimeout(r, 0))

    const putTargets = (api.put as ReturnType<typeof vi.fn>).mock.calls.map(
      (c) => c[0],
    )
    expect(putTargets).toContain('/api/theme')
    expect(putTargets).not.toContain('/api/household/features')
  })
})
