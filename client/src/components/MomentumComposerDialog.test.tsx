import { describe, it, expect } from 'vitest'

describe('MomentumComposerDialog module', () => {
  it('exports the dialog component + open helper', async () => {
    const m = await import('./MomentumComposerDialog')
    expect(typeof m.MomentumComposerDialog).toBe('function')
    expect(typeof m.openMomentumComposer).toBe('function')
  })
})
