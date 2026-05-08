import { describe, it, expect } from 'vitest'

describe('HighlightQuickShareDialog module', () => {
  it('exports the dialog component + open helper', async () => {
    const m = await import('./HighlightQuickShareDialog')
    expect(typeof m.HighlightQuickShareDialog).toBe('function')
    expect(typeof m.openHighlightQuickShare).toBe('function')
  })
})
