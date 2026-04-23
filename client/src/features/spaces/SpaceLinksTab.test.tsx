import { describe, it, expect } from 'vitest'

describe('SpaceLinksTab', () => {
  it('module exports exist', async () => {
    const mod = await import('./SpaceLinksTab')
    expect(mod).toBeTruthy()
    expect(typeof mod.SpaceLinksTab).toBe('function')
  })
})
