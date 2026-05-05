import { describe, it, expect } from 'vitest'

describe('MomentumComposerPage module', () => {
  it('exports a default component', async () => {
    const m = await import('./MomentumComposerPage')
    expect(typeof m.default).toBe('function')
  })
})
