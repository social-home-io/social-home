/**
 * StoryArchivePage — month-grid browser for retention-window stories.
 *
 * The household feed of stories at ``/stories`` shows today's rings
 * + recent items, but stories live up to the author's retention
 * setting (default 30 days) — there's a wide window of past posts
 * the inbox never surfaces. This page renders a calendar where each
 * day with a visible story is clickable; the day panel below the
 * grid lists every author + first-frame thumb for that date so the
 * user can replay it via the existing viewer.
 *
 * Data path — reuse the existing ``GET /api/stories`` endpoint. The
 * server's ``list_visible_to`` already returns every story whose
 * ``expires_at`` is in the future, i.e. the full retention window.
 * Group + sort client-side; no new endpoint, no schema change.
 */
import { useEffect, useMemo, useState } from 'preact/hooks'
import { signal, computed } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { useTitle } from '@/store/pageTitle'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { CalendarSkeleton } from '@/components/Skeleton'
import { showToast } from '@/components/Toast'
import { resolveDisplayName } from '@/utils/avatar'
import { ws } from '@/ws'
import type { StoryInboxItem } from '@/types'


type DayKey = string  // 'YYYY-MM-DD'

const inbox = signal<StoryInboxItem[]>([])
const loading = signal<boolean>(true)
const loadError = signal<string | null>(null)

/** Group every loaded story by ``story_date`` so the grid can ask
 *  ``byDay.get('2026-05-04')`` in O(1). Recomputed when inbox flips. */
const byDay = computed<Map<DayKey, StoryInboxItem[]>>(() => {
  const m = new Map<DayKey, StoryInboxItem[]>()
  for (const s of inbox.value) {
    const k = s.story.story_date
    if (!m.has(k)) m.set(k, [])
    m.get(k)!.push(s)
  }
  return m
})


function ymd(d: Date): DayKey {
  return d.toISOString().slice(0, 10)
}

function startOfMonth(year: number, month0: number): Date {
  return new Date(Date.UTC(year, month0, 1))
}


export default function StoryArchivePage() {
  useTitle('Story archive')
  const loc = useLocation()
  const today = useMemo(() => new Date(), [])
  const [year, setYear] = useState(today.getUTCFullYear())
  const [month, setMonth] = useState(today.getUTCMonth())  // 0-11
  const [selectedDay, setSelectedDay] = useState<DayKey | null>(null)

  useEffect(() => {
    loading.value = true
    loadError.value = null
    const fetchInbox = (initial: boolean) =>
      api.get('/api/stories')
        .then((rows: StoryInboxItem[]) => {
          inbox.value = rows ?? []
          if (initial) loading.value = false
        })
        .catch((err: unknown) => {
          loadError.value = (err as Error)?.message ?? 'Could not load'
          if (initial) loading.value = false
          showToast(`Failed to load story archive: ${loadError.value}`, 'error')
        })
    void fetchInbox(true)
    const dispose = [
      ws.on('story.frame_added',   () => { void fetchInbox(false) }),
      ws.on('story.frame_removed', () => { void fetchInbox(false) }),
      ws.on('story.removed',       () => { void fetchInbox(false) }),
    ]
    return () => { dispose.forEach(d => d()) }
  }, [])

  if (loading.value) return <CalendarSkeleton />

  // Build the 5×7 grid: pad with leading blanks until day-of-week
  // aligns, fill the month, pad trailing blanks to keep the layout
  // stable. Monday-first to match /calendar.
  const monthStart = startOfMonth(year, month)
  // ``getUTCDay()`` returns 0 (Sunday) ‥ 6 (Saturday); shift to
  // Mon-first.
  const lead = (monthStart.getUTCDay() + 6) % 7
  const daysInMonth = new Date(Date.UTC(year, month + 1, 0)).getUTCDate()
  const cells: Array<{ date: DayKey; inMonth: boolean } | null> = []
  for (let i = 0; i < lead; i++) cells.push(null)
  for (let d = 1; d <= daysInMonth; d++) {
    cells.push({
      date: ymd(new Date(Date.UTC(year, month, d))),
      inMonth: true,
    })
  }
  while (cells.length < 35) cells.push(null)

  const monthLabel = monthStart.toLocaleDateString(undefined, {
    year:  'numeric',
    month: 'long',
  })

  const goPrev = () => {
    setSelectedDay(null)
    if (month === 0) { setYear(year - 1); setMonth(11) }
    else setMonth(month - 1)
  }
  const goNext = () => {
    setSelectedDay(null)
    if (month === 11) { setYear(year + 1); setMonth(0) }
    else setMonth(month + 1)
  }

  // Earliest story still in the retention window — used to mute the
  // header when the rendered month falls entirely below it.
  const oldestStoryDate = inbox.value.length > 0
    ? inbox.value
        .map(s => s.story.story_date)
        .reduce((a, b) => (a < b ? a : b))
    : null
  const monthEndKey = ymd(new Date(Date.UTC(year, month + 1, 0)))
  const monthEntirelyBeforeRetention = oldestStoryDate !== null
    && monthEndKey < oldestStoryDate

  const selectedStories = selectedDay
    ? (byDay.value.get(selectedDay) ?? [])
    : []

  return (
    <div class="sh-story-archive">
      <header class="sh-story-archive-header">
        <Button variant="secondary" onClick={goPrev}>‹ Prev</Button>
        <h2 style={{ margin: 0 }}>{monthLabel}</h2>
        <Button variant="secondary" onClick={goNext}>Next ›</Button>
      </header>

      {monthEntirelyBeforeRetention && (
        <p class="sh-muted" style={{ marginTop: 0 }}>
          No stories before {oldestStoryDate} — older stories are pruned by the
          author's retention setting.
        </p>
      )}

      <div class="sh-story-archive-grid" role="grid" aria-label={monthLabel}>
        {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((d) => (
          <div key={d} class="sh-story-archive-dow" aria-hidden="true">{d}</div>
        ))}
        {cells.map((cell, i) => {
          if (cell === null) {
            return <div key={`pad-${i}`} class="sh-story-archive-day sh-story-archive-day--pad" />
          }
          const stories = byDay.value.get(cell.date) ?? []
          const hasStories = stories.length > 0
          const dayNum = Number(cell.date.slice(8, 10))
          const isToday = cell.date === ymd(today)
          const isSelected = cell.date === selectedDay
          const cls = [
            'sh-story-archive-day',
            hasStories && 'sh-story-archive-day--has',
            isToday && 'sh-story-archive-day--today',
            isSelected && 'sh-story-archive-day--selected',
          ].filter(Boolean).join(' ')

          if (!hasStories) {
            return (
              <div key={cell.date} class={cls}>
                <span class="sh-story-archive-day-num">{dayNum}</span>
              </div>
            )
          }

          // Pick the most-recent story's first frame as the cell
          // thumbnail, with a small chip when the day has more than
          // one story.
          const firstFrame = stories[0].frames[0]
          const extraCount = stories.length - 1
          return (
            <button
              key={cell.date}
              type="button"
              class={cls}
              onClick={() => setSelectedDay(cell.date)}
              aria-label={`${stories.length} ${stories.length === 1 ? 'story' : 'stories'} on ${cell.date}`}
            >
              <span class="sh-story-archive-day-num">{dayNum}</span>
              {firstFrame && firstFrame.frame_type === 'image' && (
                <img
                  src={firstFrame.media_url}
                  alt=""
                  loading="lazy"
                  class="sh-story-archive-day-thumb"
                />
              )}
              {firstFrame && firstFrame.frame_type === 'video' && (
                <span class="sh-story-archive-day-thumb sh-story-archive-day-thumb--video">
                  🎬
                </span>
              )}
              {extraCount > 0 && (
                <span class="sh-story-archive-day-more">+{extraCount}</span>
              )}
            </button>
          )
        })}
      </div>

      {selectedDay && (
        <section
          class="sh-story-archive-day-panel"
          aria-label={`Stories from ${selectedDay}`}
        >
          <h3 style={{ margin: 0 }}>
            {new Date(selectedDay + 'T00:00:00Z').toLocaleDateString(undefined, {
              weekday: 'long', month: 'short', day: 'numeric', year: 'numeric',
            })}
          </h3>
          <ul class="sh-story-archive-day-list">
            {selectedStories.map(s => {
              const author = s.story.author_user_id
              const name = resolveDisplayName(null, author, author)
              const first = s.frames[0]
              return (
                <li key={s.story.id}>
                  <a
                    href={`/stories/${s.story.id}`}
                    class="sh-story-archive-tile"
                    onClick={() => loc.route(`/stories/${s.story.id}`)}
                  >
                    {first && first.frame_type === 'image' && (
                      <img
                        src={first.media_url}
                        alt=""
                        loading="lazy"
                        class="sh-story-archive-tile-thumb"
                      />
                    )}
                    {first && first.frame_type === 'video' && (
                      <span class="sh-story-archive-tile-thumb sh-story-archive-tile-thumb--video">
                        🎬
                      </span>
                    )}
                    <div class="sh-story-archive-tile-meta">
                      <Avatar name={name} size={32} />
                      <strong>{name}</strong>
                      <span class="sh-muted">
                        {s.frames.length} frame{s.frames.length === 1 ? '' : 's'}
                      </span>
                    </div>
                  </a>
                </li>
              )
            })}
          </ul>
        </section>
      )}

      {inbox.value.length === 0 && !loading.value && (
        <div class="sh-empty-state">
          <div style={{ fontSize: '2rem' }} aria-hidden="true">🌅</div>
          <h3 style={{ margin: 0 }}>No stories in the archive yet</h3>
          <p>
            Once you or anyone in your household / connected peers post a
            story, it'll show up here for as long as the author's retention
            setting keeps it.
          </p>
        </div>
      )}
    </div>
  )
}
