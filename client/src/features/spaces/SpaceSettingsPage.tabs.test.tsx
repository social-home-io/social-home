import { describe, it, expect } from 'vitest'
import { visibleSettingsTabs } from './SpaceSettingsPage'

describe('visibleSettingsTabs', () => {
  it('non-admin members get only their own surface (Bots)', () => {
    expect(visibleSettingsTabs(false, false)).toEqual(['bots'])
    expect(visibleSettingsTabs(false, true)).toEqual(['bots'])
  })

  it('a local admin gets the full hub', () => {
    expect(visibleSettingsTabs(true, false)).toEqual([
      'general',
      'about',
      'theme',
      'links',
      'bots',
    ])
  })

  it('a remote admin gets only forwarding-capable tabs (no theme / links)', () => {
    // General (config/archive/dissolve/tier all forward) + About + Bots;
    // Theme and Quick links are host-local and must not appear on a stub.
    const tabs = visibleSettingsTabs(true, true)
    expect(tabs).toEqual(['general', 'about', 'bots'])
    expect(tabs).not.toContain('theme')
    expect(tabs).not.toContain('links')
  })
})
