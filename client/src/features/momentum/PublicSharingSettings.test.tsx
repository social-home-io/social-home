import { describe, it, expect } from 'vitest'

describe('PublicSharingSettings module', () => {
  it('exports a default component', async () => {
    const m = await import('./PublicSharingSettings')
    expect(typeof m.default).toBe('function')
  })
})
