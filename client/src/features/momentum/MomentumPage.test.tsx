import { describe, it, expect } from 'vitest'

describe('MomentumPage module', () => {
  it('exports a default component', async () => {
    const m = await import('./MomentumPage')
    expect(typeof m.default).toBe('function')
  })
})
