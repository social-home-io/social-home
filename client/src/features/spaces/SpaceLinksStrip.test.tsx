import { describe, it, expect } from 'vitest'

describe('SpaceLinksStrip', () => {
  it('module exports exist', async () => {
    const mod = await import('./SpaceLinksStrip')
    expect(mod).toBeTruthy()
    expect(typeof mod.SpaceLinksStrip).toBe('function')
  })
})
