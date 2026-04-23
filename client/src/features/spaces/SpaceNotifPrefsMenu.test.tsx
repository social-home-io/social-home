import { describe, it, expect } from 'vitest'

describe('SpaceNotifPrefsMenu', () => {
  it('module exports exist', async () => {
    const mod = await import('./SpaceNotifPrefsMenu')
    expect(mod).toBeTruthy()
    expect(typeof mod.SpaceNotifPrefsMenu).toBe('function')
  })
})
