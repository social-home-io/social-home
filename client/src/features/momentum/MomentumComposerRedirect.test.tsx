import { describe, it, expect } from 'vitest'

describe('MomentumComposerRedirect module', () => {
  it('exports a default component', async () => {
    const m = await import('./MomentumComposerRedirect')
    expect(typeof m.default).toBe('function')
  })
})
