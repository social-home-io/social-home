import { describe, it, expect } from 'vitest'

describe('OrganizePage module', () => {
  it('exports a default component', async () => {
    const m = await import('./OrganizePage')
    expect(typeof m.default).toBe('function')
  })
})
