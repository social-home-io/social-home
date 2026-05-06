import { describe, it, expect } from 'vitest'

describe('PublicDiscoveryPage module', () => {
  it('exports a default component', async () => {
    const m = await import('./PublicDiscoveryPage')
    expect(typeof m.default).toBe('function')
  })
})
