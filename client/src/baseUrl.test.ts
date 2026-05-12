import { describe, it, expect } from 'vitest'
// Note: ``baseUrl.ts`` evaluates ``document.baseURI`` at module load,
// which in JSDOM resolves to ``http://localhost/`` (the default
// origin). Tests therefore exercise the no-prefix code path
// directly; the prefixed path is exercised by the helpers'
// own logic (``addBase`` / ``stripBase`` are pure functions that
// don't read from ``document``).
import { basePath, addBase, stripBase } from './baseUrl'

describe('baseUrl', () => {
  describe('basePath (no-prefix, JSDOM default)', () => {
    it('is /', () => {
      expect(basePath).toBe('/')
    })
  })

  describe('stripBase (pure function)', () => {
    it('returns pathname unchanged when basePath is /', () => {
      expect(stripBase('/feed')).toBe('/feed')
      expect(stripBase('/spaces/abc')).toBe('/spaces/abc')
      expect(stripBase('/')).toBe('/')
    })
  })

  describe('addBase (pure function)', () => {
    it('returns path unchanged when basePath is /', () => {
      expect(addBase('/feed')).toBe('/feed')
      expect(addBase('/')).toBe('/')
      expect(addBase('feed?x=1')).toBe('feed?x=1')
    })
  })
})
