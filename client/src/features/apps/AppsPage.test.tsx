import { describe, it, expect } from 'vitest'
import { safeIconSrc } from './AppsPage'

describe('safeIconSrc', () => {
  it('returns data: URIs unchanged', () => {
    const d = 'data:image/svg+xml,%3Csvg%3E%3C/svg%3E'
    expect(safeIconSrc(d)).toBe(d)
  })

  it('returns absolute http(s) URLs unchanged', () => {
    expect(safeIconSrc('https://example.com/icon.png')).toBe('https://example.com/icon.png')
    expect(safeIconSrc('http://example.com/icon.png')).toBe('http://example.com/icon.png')
  })

  it('rejects relative paths (would 404 against the SPA origin)', () => {
    expect(safeIconSrc('icon.svg')).toBeNull()
    expect(safeIconSrc('./icon.svg')).toBeNull()
    expect(safeIconSrc('/api/apps/chess/bundle/icon.svg')).toBeNull()
  })

  it('rejects empty / null / undefined', () => {
    expect(safeIconSrc(null)).toBeNull()
    expect(safeIconSrc(undefined)).toBeNull()
    expect(safeIconSrc('')).toBeNull()
  })
})
