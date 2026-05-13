/**
 * Calendar formatting / grouping helpers shared by the household
 * calendar (`features/calendar/CalendarPage.tsx`) and the per-space
 * calendar tab (`features/spaces/SpaceFeedPage.tsx`). Keeping the
 * grouping rule in one place means the two surfaces always render the
 * same day buckets — no drift between the household and a space.
 */
import type { CalendarEvent } from '@/types'

/** Calendar view modes — mirrored on the household and per-space
 *  calendar surfaces so date math + range labels can come from one
 *  helper module. */
export type CalendarViewMode = 'month' | 'week' | 'day'

/** Group events into ``{ "M/D/YYYY" → events }`` buckets, locale-aware. */
export function groupEventsByDay(
  evts: CalendarEvent[],
): Record<string, CalendarEvent[]> {
  const groups: Record<string, CalendarEvent[]> = {}
  for (const e of evts) {
    const key = new Date(e.start).toLocaleDateString()
    if (!groups[key]) groups[key] = []
    groups[key].push(e)
  }
  return groups
}

/** Friendly day-group label — "Today · Friday 8 May" / "Tomorrow ·
 *  Saturday 9 May" / "Mon 12 May" rather than the spreadsheet-y
 *  ``5/8/2026`` the toLocaleDateString default emits. ``dayKey`` is
 *  the ``toLocaleDateString()`` output the grouping uses; we round-
 *  trip through ``Date`` to get a stable rendering. */
export interface FriendlyDayLabel {
  /** Long form for visual rendering: "Friday 8 May" */
  long: string
  /** Relative descriptor for the kicker: "Today" / "Tomorrow" / null */
  relative: string | null
  /** True when the bucket is the local current day. */
  isToday: boolean
}
export function formatDayLabel(dayKey: string): FriendlyDayLabel {
  const date = new Date(dayKey)
  // ``new Date('5/8/2026')`` parses across en-* locales; for other
  // locales we fall through to the raw key. Anyone hitting an
  // unparseable key gets the original string back rather than
  // "Invalid Date".
  if (Number.isNaN(date.getTime())) {
    return { long: dayKey, relative: null, isToday: false }
  }
  const today = new Date()
  const sameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear()
    && a.getMonth() === b.getMonth()
    && a.getDate() === b.getDate()
  const tomorrow = new Date(today)
  tomorrow.setDate(today.getDate() + 1)
  const isToday = sameDay(date, today)
  const isTomorrow = sameDay(date, tomorrow)
  const long = date.toLocaleDateString(undefined, {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
  })
  const relative = isToday
    ? 'Today'
    : isTomorrow
      ? 'Tomorrow'
      : null
  return { long, relative, isToday }
}

/** Heading for a month-view month strip — "April 2026", localised. */
export function formatMonthHeading(date: Date): string {
  return date.toLocaleDateString(undefined, {
    month: 'long',
    year: 'numeric',
  })
}

/** ISO bounds for the calendar month containing ``date``. */
export function monthRange(date: Date): { start: string; end: string } {
  const start = new Date(date.getFullYear(), date.getMonth(), 1)
  const end = new Date(date.getFullYear(), date.getMonth() + 1, 0, 23, 59, 59)
  return { start: start.toISOString(), end: end.toISOString() }
}

/** ISO bounds covering the active period for ``mode`` anchored at
 *  ``date``. Month → calendar month; week → Sun-Sat; day → 00:00 to
 *  23:59:59. Used by both the household calendar and the per-space
 *  calendar's view-mode switcher. */
export function dateRangeForMode(
  date: Date,
  mode: CalendarViewMode,
): { start: string; end: string } {
  if (mode === 'month') return monthRange(date)
  const d = new Date(date)
  if (mode === 'week') {
    const dayOfWeek = d.getDay()
    const start = new Date(d)
    start.setDate(d.getDate() - dayOfWeek)
    start.setHours(0, 0, 0, 0)
    const end = new Date(start)
    end.setDate(start.getDate() + 6)
    end.setHours(23, 59, 59, 0)
    return { start: start.toISOString(), end: end.toISOString() }
  }
  // day
  const start = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const end = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 23, 59, 59)
  return { start: start.toISOString(), end: end.toISOString() }
}

/** Heading shown in the controls strip for the active period. */
export function formatRangeHeading(
  date: Date,
  mode: CalendarViewMode,
): string {
  if (mode === 'month') return formatMonthHeading(date)
  if (mode === 'week') {
    const start = new Date(date)
    start.setDate(date.getDate() - date.getDay())
    const end = new Date(start)
    end.setDate(start.getDate() + 6)
    return `${start.toLocaleDateString(undefined, {
      month: 'short', day: 'numeric',
    })} – ${end.toLocaleDateString(undefined, {
      month: 'short', day: 'numeric', year: 'numeric',
    })}`
  }
  return date.toLocaleDateString(undefined, {
    weekday: 'long', month: 'long', day: 'numeric', year: 'numeric',
  })
}

/** ``date`` advanced by ``direction`` units of ``mode`` (-1 = back). */
export function advanceDate(
  date: Date,
  direction: number,
  mode: CalendarViewMode,
): Date {
  const next = new Date(date)
  if (mode === 'month') next.setMonth(next.getMonth() + direction)
  else if (mode === 'week') next.setDate(next.getDate() + 7 * direction)
  else next.setDate(next.getDate() + direction)
  return next
}

/** Deterministic colour per calendar id — picks one of 16 hand-tuned
 *  earth-tone hues so two members rarely collide visually. The same id
 *  always lands on the same colour across reloads / sessions. 16 is
 *  enough for any realistic household; if a household ever has 17+
 *  calendars the chip names still disambiguate. */
const _CAL_HUES = [
  'var(--sh-primary)',  // terracotta
  'var(--sh-success)',  // moss
  'var(--sh-warning)',  // honey
  'var(--sh-danger)',   // brick
  '#7B5BA8',            // plum
  '#3F7B8C',            // dusty teal
  '#A89344',            // ochre
  '#5C7B5A',            // sage
  '#9B5B3F',            // cinnamon
  '#34688D',            // navy
  '#7C9D5F',            // olive
  '#B57E47',            // amber
  '#5B8E8E',            // slate teal
  '#8C5777',            // rose-plum
  '#46735A',            // pine
  '#BC6C68',            // brick rose
] as const

export function calendarHue(calId: string): string {
  // Tiny string-hash → pick a hue. djb2-flavoured.
  let h = 5381
  for (let i = 0; i < calId.length; i++) {
    h = ((h << 5) + h + calId.charCodeAt(i)) | 0
  }
  return _CAL_HUES[Math.abs(h) % _CAL_HUES.length]
}

/** Resolve the chip dot colour. The DB column wins when the owner has
 *  picked one. Both legacy "default-blue" sentinels (``#4A90E2`` from the
 *  schema, ``#2196F3`` from an earlier service default) are treated as
 *  "unset" so the warm hash-derived palette takes over — leaving every
 *  fresh calendar a cold-blue chip read as generic and stale against
 *  the hearth surface. */
const _UNSET_CAL_COLORS = new Set(['#4a90e2', '#2196f3'])
export function resolveCalendarColor(
  c: { id: string; color?: string | null },
): string {
  if (c.color && !_UNSET_CAL_COLORS.has(c.color.toLowerCase())) return c.color
  return calendarHue(c.id)
}
