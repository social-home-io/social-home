import { describe, it, expect } from 'vitest'
import {
  formatDayLabel,
  formatMonthHeading,
  groupEventsByDay,
  groupSharedEvents,
  monthRange,
} from './calendar'
import type { CalendarEvent } from '@/types'

interface SharedEvtOpts {
  calendar_id?: string
  summary?: string
  start?: string
  end?: string
  created_by?: string
  description?: string | null
  location?: string | null
  cover_url?: string | null
}
function sharedEvt(id: string, options: SharedEvtOpts = {}): CalendarEvent {
  return {
    id,
    calendar_id: 'cal-a',
    summary: 's',
    description: null,
    start: '2026-05-15T18:00:00Z',
    end: '2026-05-15T19:00:00Z',
    all_day: false,
    rrule: null,
    capacity: null,
    created_by: 'u-alice',
    location: null,
    cover_url: null,
    ...options,
  } as unknown as CalendarEvent
}

function evt(id: string, startISO: string): CalendarEvent {
  return {
    id,
    calendar_id: 'cal-1',
    summary: id,
    description: null,
    start: startISO,
    end: startISO,
    all_day: false,
    rrule: null,
    capacity: null,
    created_by: 'u-1',
  } as unknown as CalendarEvent
}

describe('calendar utils', () => {
  it('groups events by their local-date key, preserving order within a day', () => {
    const a = evt('a', '2026-04-30T08:00:00')
    const b = evt('b', '2026-04-30T15:00:00')
    const c = evt('c', '2026-05-01T09:00:00')
    const groups = groupEventsByDay([a, b, c])
    const keys = Object.keys(groups)
    expect(keys.length).toBe(2)
    expect(groups[keys[0]].map(e => e.id)).toEqual(['a', 'b'])
    expect(groups[keys[1]].map(e => e.id)).toEqual(['c'])
  })

  it('formats a month heading with month name + year', () => {
    const heading = formatMonthHeading(new Date('2026-04-15T00:00:00'))
    expect(heading.toLowerCase()).toContain('april')
    expect(heading).toContain('2026')
  })

  it('returns ISO bounds covering the whole calendar month', () => {
    const { start, end } = monthRange(new Date('2026-04-15T12:00:00'))
    expect(new Date(start).getDate()).toBe(1)
    expect(new Date(start).getMonth()).toBe(3) // April = 3 (0-indexed)
    // Last day of April is the 30th.
    expect(new Date(end).getDate()).toBe(30)
    expect(new Date(end).getMonth()).toBe(3)
  })
})

describe('formatDayLabel', () => {
  it('marks the current day with the "Today" relative kicker', () => {
    const today = new Date()
    const key = today.toLocaleDateString()
    const out = formatDayLabel(key)
    expect(out.isToday).toBe(true)
    expect(out.relative).toBe('Today')
    // Long form contains the weekday — locale-agnostic existence check.
    expect(out.long.length).toBeGreaterThan(5)
  })

  it('marks the day after as "Tomorrow"', () => {
    const tomorrow = new Date()
    tomorrow.setDate(tomorrow.getDate() + 1)
    const out = formatDayLabel(tomorrow.toLocaleDateString())
    expect(out.isToday).toBe(false)
    expect(out.relative).toBe('Tomorrow')
  })

  it('returns null relative for far-future / past days', () => {
    const future = new Date()
    future.setDate(future.getDate() + 7)
    const out = formatDayLabel(future.toLocaleDateString())
    expect(out.isToday).toBe(false)
    expect(out.relative).toBe(null)
  })

  it('falls back to the original key on unparseable input', () => {
    const out = formatDayLabel('not-a-date')
    expect(out.long).toBe('not-a-date')
    expect(out.relative).toBe(null)
  })
})

describe('groupSharedEvents', () => {
  const cals = [
    { id: 'cal-a', owner_username: 'alice' },
    { id: 'cal-b', owner_username: 'bob' },
    { id: 'cal-c', owner_username: 'carol' },
  ]

  it('merges multi-calendar fan-out of one event into a single row', () => {
    const onAlice = sharedEvt('e-1', { calendar_id: 'cal-a' })
    const onBob = sharedEvt('e-2', { calendar_id: 'cal-b' })
    const out = groupSharedEvents([onAlice, onBob], cals)
    expect(out).toHaveLength(1)
    // Primary is the creator's row (alice owns cal-a, created_by=u-alice).
    expect(out[0].id).toBe('e-1')
    expect(out[0].calendar_id).toBe('cal-a')
    expect(out[0]._grouped_calendar_ids).toEqual(['cal-a', 'cal-b'])
    expect(out[0]._grouped_event_ids).toEqual(['e-1', 'e-2'])
  })

  it('preserves single-row events unchanged (no group metadata added)', () => {
    const lonely = sharedEvt('lonely', { calendar_id: 'cal-a' })
    const out = groupSharedEvents([lonely], cals)
    expect(out).toHaveLength(1)
    expect(out[0]).toBe(lonely) // same reference — no clone for singletons
    expect(out[0]._grouped_calendar_ids).toBeUndefined()
  })

  it('keeps genuinely different same-minute twins separate', () => {
    // Two events at the same time / creator, different titles — NOT a
    // merge (e.g. parallel after-school activities).
    const a = sharedEvt('e-tennis', {
      calendar_id: 'cal-a', summary: 'Tennis with Pascal',
    })
    const b = sharedEvt('e-piano', {
      calendar_id: 'cal-b', summary: 'Piano with Maria',
    })
    const out = groupSharedEvents([a, b], cals)
    expect(out).toHaveLength(2)
  })

  it('keeps same-title twins with different locations separate', () => {
    const home = sharedEvt('e-home', {
      calendar_id: 'cal-a', location: 'Kitchen',
    })
    const out_a = sharedEvt('e-out', {
      calendar_id: 'cal-b', location: 'Café',
    })
    const out = groupSharedEvents([home, out_a], cals)
    expect(out).toHaveLength(2)
  })

  it('does not merge across creators even when title and time match', () => {
    const byAlice = sharedEvt('e-1', {
      calendar_id: 'cal-a', created_by: 'u-alice',
    })
    const byBob = sharedEvt('e-2', {
      calendar_id: 'cal-b', created_by: 'u-bob',
    })
    const out = groupSharedEvents([byAlice, byBob], cals)
    expect(out).toHaveLength(2)
  })

  it("falls back to the first row when the creator's calendar is hidden", () => {
    // Alice's row is missing from the input (the user filtered her
    // calendar out); only Bob's row is visible. Group still resolves
    // — Bob's row becomes the primary even though Alice created it.
    const onBob = sharedEvt('e-bob', {
      calendar_id: 'cal-b', created_by: 'u-alice',
    })
    const onCarol = sharedEvt('e-carol', {
      calendar_id: 'cal-c', created_by: 'u-alice',
    })
    const out = groupSharedEvents([onBob, onCarol], cals)
    expect(out).toHaveLength(1)
    expect(out[0].id).toBe('e-bob') // first row in input
    expect(out[0]._grouped_calendar_ids).toEqual(['cal-b', 'cal-c'])
  })

  it('returns an empty array unchanged', () => {
    expect(groupSharedEvents([], cals)).toEqual([])
  })

  // ── client_event_uuid path (issue #327) ──────────────────────────────

  it('merges rows that share a client_event_uuid even when titles diverge', () => {
    // The intent-driven path: same uuid → same event, no matter what
    // the user did to the title / description / location on one row
    // post-creation. Without the uuid this case would split because
    // the content key disagrees.
    const aliceRow = sharedEvt('e-1', {
      calendar_id: 'cal-a',
      summary: 'Family dinner',
    } as SharedEvtOpts & { client_event_uuid?: string }) as CalendarEvent
    ;(aliceRow as { client_event_uuid?: string }).client_event_uuid
      = 'abcdef0123456789abcdef0123456789'
    const bobRow = sharedEvt('e-2', {
      calendar_id: 'cal-b',
      summary: 'Family dinner (edited only on Bob\'s row)',
    } as SharedEvtOpts) as CalendarEvent
    ;(bobRow as { client_event_uuid?: string }).client_event_uuid
      = 'abcdef0123456789abcdef0123456789'
    const out = groupSharedEvents([aliceRow, bobRow], cals)
    expect(out).toHaveLength(1)
    expect(out[0]._grouped_calendar_ids).toEqual(['cal-a', 'cal-b'])
    expect(out[0]._grouped_event_ids).toEqual(['e-1', 'e-2'])
  })

  it('does not merge two rows with different client_event_uuids', () => {
    // Two genuinely different events that happen to share a content
    // key — the uuid disambiguates them and keeps them separate.
    const a = sharedEvt('e-1', { calendar_id: 'cal-a' }) as CalendarEvent
    ;(a as { client_event_uuid?: string }).client_event_uuid
      = '11111111111111111111111111111111'
    const b = sharedEvt('e-2', { calendar_id: 'cal-b' }) as CalendarEvent
    ;(b as { client_event_uuid?: string }).client_event_uuid
      = '22222222222222222222222222222222'
    const out = groupSharedEvents([a, b], cals)
    expect(out).toHaveLength(2)
  })

  it('groups uuid-only rows together with no calendars table', () => {
    // Cross-household case: the recipient hasn't synced the host's
    // ``calendars`` row but DID receive the federation envelope.
    // ``calendars`` arg is empty; uuid alone still groups.
    const a = sharedEvt('e-1', { calendar_id: 'cal-a' }) as CalendarEvent
    ;(a as { client_event_uuid?: string }).client_event_uuid
      = 'abcdef0123456789abcdef0123456789'
    const b = sharedEvt('e-2', { calendar_id: 'cal-x-remote' }) as CalendarEvent
    ;(b as { client_event_uuid?: string }).client_event_uuid
      = 'abcdef0123456789abcdef0123456789'
    const out = groupSharedEvents([a, b], [])
    expect(out).toHaveLength(1)
    expect(out[0]._grouped_event_ids).toEqual(['e-1', 'e-2'])
  })

  it('keeps a uuid-bearing row separate from a uuid-less twin', () => {
    // Mixed case during rollout: the host stamped a uuid on the new
    // event but the recipient is on a sub-version peer that stripped
    // it. We don't try to bridge the two — content-key fallback
    // would, but the lack of uuid on one side means we can't trust
    // any cross-version merge.
    const stamped = sharedEvt('e-1', { calendar_id: 'cal-a' }) as CalendarEvent
    ;(stamped as { client_event_uuid?: string }).client_event_uuid
      = 'abcdef0123456789abcdef0123456789'
    const unstamped = sharedEvt('e-2', { calendar_id: 'cal-b' }) as CalendarEvent
    const out = groupSharedEvents([stamped, unstamped], cals)
    // One uuid-group + one content-group → two rows. The agenda
    // shows both with their own chip, which is honest about the
    // mixed-version state.
    expect(out).toHaveLength(2)
  })
})
