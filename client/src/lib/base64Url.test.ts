import { describe, it, expect } from 'vitest'
import { base64UrlEncode, base64UrlDecode } from './base64Url'

describe('base64Url', () => {
  it('round-trips ASCII', () => {
    const enc = base64UrlEncode('hello world')
    expect(base64UrlDecode(enc)).toBe('hello world')
  })

  it('round-trips unicode (emoji + accents)', () => {
    const input = '🏠 ümlaut · 大丈夫'
    expect(base64UrlDecode(base64UrlEncode(input))).toBe(input)
  })

  it('round-trips a JSON payload (the actual use case)', () => {
    const payload = JSON.stringify({
      token: '0123456789abcdef',
      space_id: 'space-1',
      space_display_hint: "Pascal's family",
    })
    expect(base64UrlDecode(base64UrlEncode(payload))).toBe(payload)
  })

  it('emits no `+`, `/`, or `=` characters', () => {
    // Bytes that would normally produce all three: 0xFB 0xFF 0xBF.
    const tricky = 'ûÿ¿'
    const enc = base64UrlEncode(tricky)
    expect(enc).not.toMatch(/[+/=]/)
    expect(base64UrlDecode(enc)).toBe(tricky)
  })

  it('decodes strings with padding stripped (canonical RFC-4648 §5)', () => {
    // ``man`` → ``bWFu`` in normal base64 (no padding).
    expect(base64UrlDecode('bWFu')).toBe('man')
    // ``ma`` → ``bWE=`` normally; URL form strips the trailing ``=``.
    expect(base64UrlDecode('bWE')).toBe('ma')
    // ``m`` → ``bQ==`` normally; URL form strips both ``==``.
    expect(base64UrlDecode('bQ')).toBe('m')
  })

  it('accepts strings with `-` and `_` substituted for `+`/`/`', () => {
    // 0xFB 0xFF 0xBF → ``+/+/`` in base64 → ``-_-_`` in URL form.
    const tricky = 'ûÿ¿'
    expect(base64UrlDecode(base64UrlEncode(tricky))).toBe(tricky)
  })

  it('returns null for malformed input rather than throwing', () => {
    // ``!@#`` is not valid base64.
    expect(base64UrlDecode('!@#$%')).toBeNull()
  })
})
