import { describe, it, expect } from 'vitest'

describe('UserActionsMenu', () => {
  it('module exports exist', async () => {
    const mod = await import('./UserActionsMenu')
    expect(typeof mod.UserActionsMenu).toBe('function')
    expect(typeof mod.openUserActions).toBe('function')
  })
})
