/**
 * Tests for the timezone helper module. Two load-bearing invariants
 * the calendar fix rests on:
 *
 * 1. ``localPartsToUtcIso`` converts a `(date, time, tz)` triple to
 *    UTC ISO **correctly across DST** — the legacy fake-Z code in the
 *    create dialog couldn't, which was the root cause of every event
 *    being off by the creator's UTC offset.
 *
 * 2. ``formatEventTime`` renders the event in its originating tz with
 *    a "your time" annotation only when the viewer's tz differs — so
 *    same-household viewing stays uncluttered while cross-household
 *    viewing surfaces the shared anchor clearly.
 */
import { describe, it, expect } from 'vitest'
import {
  detectBrowserTz,
  formatEventTime,
  localPartsToUtcIso,
  utcIsoToLocalParts,
} from './timezone'

describe('detectBrowserTz', () => {
  it('returns a non-empty string', () => {
    const tz = detectBrowserTz()
    expect(typeof tz).toBe('string')
    expect(tz.length).toBeGreaterThan(0)
  })
})

describe('localPartsToUtcIso — DST correctness', () => {
  it('treats wall-clock 19:00 Europe/Berlin in winter as 18:00Z', () => {
    // CET (UTC+1) — Berlin is one hour ahead of UTC in winter.
    const iso = localPartsToUtcIso('2026-01-15', '19:00', 'Europe/Berlin')
    expect(iso).toBe('2026-01-15T18:00:00.000Z')
  })

  it('treats wall-clock 19:00 Europe/Berlin in summer as 17:00Z', () => {
    // CEST (UTC+2) — Berlin is two hours ahead of UTC in summer. The
    // legacy fake-Z code would have emitted ``2026-07-15T19:00:00Z``,
    // which is the bug we're fixing.
    const iso = localPartsToUtcIso('2026-07-15', '19:00', 'Europe/Berlin')
    expect(iso).toBe('2026-07-15T17:00:00.000Z')
  })

  it('handles 09:00 America/New_York after fall-back DST end', () => {
    // After Nov 2 2025 02:00 the zone is EST (UTC-5).
    const iso = localPartsToUtcIso('2025-11-15', '09:00', 'America/New_York')
    expect(iso).toBe('2025-11-15T14:00:00.000Z')
  })

  it('handles 09:00 America/New_York during summer EDT (UTC-4)', () => {
    const iso = localPartsToUtcIso('2025-07-15', '09:00', 'America/New_York')
    expect(iso).toBe('2025-07-15T13:00:00.000Z')
  })

  it('UTC tz roundtrips unchanged', () => {
    expect(localPartsToUtcIso('2026-04-01', '12:30', 'UTC')).toBe(
      '2026-04-01T12:30:00.000Z',
    )
  })
})

describe('utcIsoToLocalParts', () => {
  it('renders Berlin wall clock for a UTC instant in winter', () => {
    expect(utcIsoToLocalParts('2026-01-15T18:00:00Z', 'Europe/Berlin')).toEqual({
      date: '2026-01-15',
      time: '19:00',
    })
  })

  it('renders New York wall clock honouring DST', () => {
    expect(utcIsoToLocalParts('2025-07-15T13:00:00Z', 'America/New_York'))
      .toEqual({ date: '2025-07-15', time: '09:00' })
    expect(utcIsoToLocalParts('2025-11-15T14:00:00Z', 'America/New_York'))
      .toEqual({ date: '2025-11-15', time: '09:00' })
  })
})

describe('formatEventTime', () => {
  // The `primary` string is locale-formatted via the test runner's
  // default locale (en-US in CI → 12h "7:00 PM", many EU locales →
  // 24h "19:00"). Either is correct; the assertions below match
  // "wall-clock 19:00 Berlin" with both shapes.
  const WALL_19_BERLIN = /(19:00|7:00\s*PM)/i
  const WALL_13_NY = /(13:00|1:00\s*PM)/i

  it('omits the "your time" line when event tz == viewer tz', () => {
    const out = formatEventTime(
      '2026-01-15T18:00:00Z',
      'Europe/Berlin',
      'Europe/Berlin',
    )
    expect(out.secondary).toBeNull()
    expect(out.primary).toMatch(WALL_19_BERLIN)
    expect(out.primaryTz).toBe('Europe/Berlin')
  })

  it('attaches the "your time" line when tzs differ', () => {
    // 18:00Z → 19:00 Berlin → 13:00 NY (winter).
    const out = formatEventTime(
      '2026-01-15T18:00:00Z',
      'Europe/Berlin',
      'America/New_York',
    )
    expect(out.primary).toMatch(WALL_19_BERLIN)
    expect(out.primaryTz).toBe('Europe/Berlin')
    expect(out.secondary).toMatch(/your time/)
    expect(out.secondary).toMatch(WALL_13_NY)
  })
})
