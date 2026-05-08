/**
 * Relative-time helpers for feed-style surfaces.
 *
 * Five surfaces (DM inbox, Pages index, Pages viewer, Notifications,
 * follow-on candidates) had each inlined a lightly-tweaked relative
 * formatter. The shapes split into two families that the SPA actually
 * needs:
 *
 * 1. **Compact ("chat" shape)** — for dense lists like the DM inbox or
 *    the Momentum feed where a one-or-two-character pill is what the
 *    user is scanning. ``now`` / ``5m`` / ``3h`` / ``Yesterday`` /
 *    ``Mon`` / ``Apr 23``.
 * 2. **Verbose ("docs" shape)** — for surfaces where the row has space
 *    for a friendly phrase like a Pages byline or a notification row.
 *    ``just now`` / ``5 min ago`` / ``3h ago`` / ``yesterday`` /
 *    ``5 days ago`` / ``Apr 23``.
 *
 * Both fall back to the raw input on parse failure (rare — server
 * timestamps are always RFC 3339). The surfaces still pair the result
 * with a ``time[title]`` carrying the precise locale string so a hover
 * or screen-reader still gets the full stamp.
 */

const MS_PER_MIN = 60_000
const MS_PER_DAY = 86_400_000

interface ParsedDelta {
  t: number
  now: number
  diff: number
  min: number
  hr: number
  sameDay: boolean
  yesterday: boolean
}

function parseDelta(iso: string): ParsedDelta | null {
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return null
  const now = Date.now()
  const diff = now - t
  const dayThen = new Date(t).toDateString()
  const dayNow = new Date(now).toDateString()
  const yesterdayDate = new Date(now)
  yesterdayDate.setDate(yesterdayDate.getDate() - 1)
  return {
    t,
    now,
    diff,
    min: Math.floor(diff / MS_PER_MIN),
    hr: Math.floor(diff / MS_PER_MIN / 60),
    sameDay: dayThen === dayNow,
    yesterday: dayThen === yesterdayDate.toDateString(),
  }
}

/**
 * Compact "chat" shape. Use for tight scan-by lists (DM inbox, momentum
 * feed). One or two characters wherever possible.
 */
export function relativeChatTime(iso: string): string {
  const d = parseDelta(iso)
  if (!d) return iso
  if (d.min < 1) return 'now'
  if (d.min < 60) return `${d.min}m`
  if (d.sameDay) return `${d.hr}h`
  if (d.yesterday) return 'Yesterday'
  if (d.diff < 6 * MS_PER_DAY) {
    return new Date(d.t).toLocaleDateString(undefined, { weekday: 'short' })
  }
  return new Date(d.t).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
  })
}

/**
 * Verbose "docs" shape. Use for surfaces that have room for a friendly
 * phrase (Pages byline, Notifications row).
 */
export function relativeDocsTime(iso: string): string {
  const d = parseDelta(iso)
  if (!d) return iso
  if (d.min < 1) return 'just now'
  if (d.min < 60) return `${d.min} min ago`
  if (d.sameDay) return `${d.hr}h ago`
  if (d.yesterday) return 'yesterday'
  if (d.diff < 7 * MS_PER_DAY) {
    return `${Math.floor(d.diff / MS_PER_DAY)} days ago`
  }
  const sameYear = new Date(d.t).getFullYear() === new Date(d.now).getFullYear()
  return new Date(d.t).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: sameYear ? undefined : 'numeric',
  })
}
