import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { relativeChatTime, relativeDocsTime } from './relativeTime'

const FIXED_NOW = new Date('2026-05-08T13:00:00Z').getTime()

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(FIXED_NOW)
})
afterEach(() => {
  vi.useRealTimers()
})

const iso = (offsetMs: number) => new Date(FIXED_NOW - offsetMs).toISOString()

describe('relativeChatTime', () => {
  it('renders "now" under a minute', () => {
    expect(relativeChatTime(iso(15_000))).toBe('now')
  })
  it('renders ``Nm`` minutes within the hour', () => {
    expect(relativeChatTime(iso(5 * 60_000))).toBe('5m')
  })
  it('renders ``Nh`` hours within the same calendar day', () => {
    expect(relativeChatTime(iso(3 * 3_600_000))).toBe('3h')
  })
  it('renders "Yesterday" for a stamp that crosses one calendar boundary', () => {
    // 30h ago lands on the previous calendar day (with FIXED_NOW @ 13:00 UTC).
    expect(relativeChatTime(iso(30 * 3_600_000))).toBe('Yesterday')
  })
  it('renders the weekday name for last 6 days', () => {
    expect(relativeChatTime(iso(3 * 86_400_000))).toMatch(/^[A-Za-z]{3}$/)
  })
  it('renders ``Mon D`` past the weekday window', () => {
    // 14 days ago — should land on a "MMM D" shape.
    const out = relativeChatTime(iso(14 * 86_400_000))
    expect(out).toMatch(/^[A-Za-z]{3,} \d+$/)
  })
  it('echoes the input on a parse failure', () => {
    expect(relativeChatTime('not-a-date')).toBe('not-a-date')
  })
})

describe('relativeDocsTime', () => {
  it('renders "just now" under a minute', () => {
    expect(relativeDocsTime(iso(15_000))).toBe('just now')
  })
  it('renders "N min ago" within the hour', () => {
    expect(relativeDocsTime(iso(5 * 60_000))).toBe('5 min ago')
  })
  it('renders "Nh ago" within the same calendar day', () => {
    expect(relativeDocsTime(iso(3 * 3_600_000))).toBe('3h ago')
  })
  it('renders "yesterday" for a stamp that crosses one calendar boundary', () => {
    expect(relativeDocsTime(iso(30 * 3_600_000))).toBe('yesterday')
  })
  it('renders "N days ago" inside the week', () => {
    expect(relativeDocsTime(iso(3 * 86_400_000))).toBe('3 days ago')
  })
  it('echoes the input on a parse failure', () => {
    expect(relativeDocsTime('not-a-date')).toBe('not-a-date')
  })
})
