/**
 * Tests for HouseholdToggles — verifies the module exports and that the
 * Toggles interface includes Presence/Gallery and NOT Highlights/Momentum.
 *
 * The component loads data from ``/api/household/preferences`` and sends
 * updates to the same endpoint via PUT.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('@/api', () => ({
  api: {
    get: vi.fn().mockResolvedValue({
      feat_feed: true, feat_pages: true, feat_tasks: true,
      feat_stickies: true, feat_calendar: true,
      feat_presence: true, feat_gallery: true,
      allow_text: true, allow_image: true, allow_video: true,
      allow_file: true, allow_poll: true, allow_schedule: true,
      allow_highlight_share: true,
      household_name: 'Test Household',
    }),
    put: vi.fn().mockResolvedValue({}),
  },
}))

vi.mock('@/ws', () => ({
  ws: {
    on: vi.fn().mockReturnValue(() => {}),
  },
}))

vi.mock('./Toast', () => ({
  showToast: vi.fn(),
}))

describe('HouseholdToggles', () => {
  beforeEach(async () => {
    // Reset the module-level signal between tests
    const mod = await import('./HouseholdToggles')
    mod.toggles.value = null
  })

  it('module exports exist (loadToggles, toggles, HouseholdToggles)', async () => {
    const mod = await import('./HouseholdToggles')
    expect(mod.loadToggles).toBeTruthy()
    expect(mod.toggles).toBeTruthy()
    expect(mod.HouseholdToggles).toBeTruthy()
  })

  it('loadToggles fetches from /api/household/preferences', async () => {
    const { api } = await import('@/api')
    const { loadToggles } = await import('./HouseholdToggles')
    await loadToggles()
    expect(api.get).toHaveBeenCalledWith('/api/household/preferences')
  })

  it('loadToggles populates feat_presence and feat_gallery', async () => {
    const { loadToggles, toggles } = await import('./HouseholdToggles')
    await loadToggles()
    expect(toggles.value?.feat_presence).toBe(true)
    expect(toggles.value?.feat_gallery).toBe(true)
  })

  it('toggles signal does NOT include feat_highlights or feat_momentum after load', async () => {
    const { loadToggles, toggles } = await import('./HouseholdToggles')
    await loadToggles()
    // The Toggles interface no longer defines these fields; they must
    // not appear in the populated signal.
    expect((toggles.value as unknown as Record<string, unknown>)['feat_highlights']).toBeUndefined()
    expect((toggles.value as unknown as Record<string, unknown>)['feat_momentum']).toBeUndefined()
  })
})
