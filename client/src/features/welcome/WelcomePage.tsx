/**
 * WelcomePage — the warm "open the door" landing surface (§Welcome).
 *
 * Mounted at ``/`` by :mod:`LandingDispatch` for any user who hasn't
 * picked a different home page in Settings.  Optimised for the
 * "person just opened the app" moment — the goal is for the first
 * three seconds to answer:
 *
 *   1. Did the app remember me? → time-of-day greeting + first name.
 *   2. What's actually on today? → today's events + pending tasks,
 *      content-conditional (cards collapse when empty rather than
 *      showing nag-ish "no events today" placeholders).
 *   3. Anything I missed? → catch-up tray with the most recent post
 *      from a followed space + chips for unread DMs / notifications.
 *
 * Deliberate non-goals — we are not the full corner.  The corner page
 * (``/corner`` / :class:`DashboardPage`) keeps the bazaar widget,
 * presence map, network map, quick actions, etc.  This page stays a
 * single column of paper-card stripes so the *glance* is the whole
 * experience.  No sidebar entry — users discover this surface only by
 * opening the app at ``/``.
 *
 * Data: reuses ``GET /api/me/corner`` (same endpoint as DashboardPage,
 * so the SPA only ever hits one bundle endpoint regardless of which
 * landing the user prefers).  Today-only filtering happens here on the
 * client; the bundle is small enough (≤~50 rows total) that we don't
 * need a dedicated `/api/me/welcome` slice.
 */
import { useEffect, useState } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import { api } from '@/api'
import { ws } from '@/ws'
import { currentUser } from '@/store/auth'
import { Avatar } from '@/components/Avatar'
import { CardSkeleton } from '@/components/SkeletonScreen'

// ─── Types — mirror DashboardPage's CornerBundle shape ─────────────

interface WelcomeEvent {
  id: string
  summary: string
  start: string
  end: string
  all_day: boolean
}
interface WelcomeTask {
  id: string
  list_id: string
  title: string
  status: 'todo' | 'in_progress' | 'done'
  due_date: string | null
}
interface WelcomeFollowedPost {
  post_id: string
  space_id: string
  space_name: string
  space_emoji: string | null
  author: string
  type: string
  content: string | null
  created_at: string
}
interface WelcomeBundle {
  unread_notifications: number
  unread_conversations: number
  upcoming_events: WelcomeEvent[]
  tasks_due_today: WelcomeTask[]
  followed_spaces_feed: WelcomeFollowedPost[]
}

// ─── Time / formatting helpers ─────────────────────────────────────

/** Pick a time-of-day-aware greeting.  Uses device-local hour because
 *  the welcome line is anchored to "what the user is doing now", not
 *  to server UTC. */
function timeOfDayGreeting(): string {
  const h = new Date().getHours()
  if (h < 5)  return 'Good night'
  if (h < 12) return 'Good morning'
  if (h < 17) return 'Good afternoon'
  if (h < 22) return 'Good evening'
  return 'Good night'
}

/** "Pascal Vizeli" → "Pascal".  Single-word names pass through. */
function firstName(displayName: string | undefined | null): string {
  if (!displayName) return ''
  const trimmed = displayName.trim()
  const sp = trimmed.indexOf(' ')
  return sp === -1 ? trimmed : trimmed.slice(0, sp)
}

/** Long-form date — "Friday, May 8".  No year (shouting "2026" at
 *  the user every morning isn't warm). */
function longDate(d: Date): string {
  return d.toLocaleDateString(undefined, {
    weekday: 'long', month: 'long', day: 'numeric',
  })
}

/** "08:30" — local time, 24h-aware via the locale.  Returns "" for
 *  all-day events; the caller handles the all-day formatting. */
function eventTime(e: WelcomeEvent): string {
  if (e.all_day) return ''
  return new Date(e.start).toLocaleTimeString(undefined, {
    hour: '2-digit', minute: '2-digit',
  })
}

/** Filter the corner's ``upcoming_events`` down to events that start
 *  on the local-today date.  All-day events for today are kept. */
function todaysEvents(events: WelcomeEvent[]): WelcomeEvent[] {
  const todayKey = new Date().toDateString()
  return events.filter(e => new Date(e.start).toDateString() === todayKey)
}

/** When today is empty, fall back to the next 1-2 events within the
 *  upcoming window so the welcome surface still answers "what's
 *  next?".  The corner endpoint already filters to upcoming-only,
 *  so the input here is naturally future-only. */
function nextEvents(events: WelcomeEvent[]): WelcomeEvent[] {
  return events.slice(0, 2)
}

/** "Tomorrow" / "Mon" / "May 12" — date label for non-today rows. */
function dayLabel(iso: string): string {
  const d = new Date(iso)
  const today = new Date()
  const tomorrow = new Date(today)
  tomorrow.setDate(today.getDate() + 1)
  if (d.toDateString() === tomorrow.toDateString()) return 'Tomorrow'
  const diffDays = Math.floor(
    (d.setHours(0, 0, 0, 0) - new Date().setHours(0, 0, 0, 0)) / 86_400_000,
  )
  if (diffDays > 0 && diffDays < 7) {
    return new Date(iso).toLocaleDateString(undefined, { weekday: 'long' })
  }
  return new Date(iso).toLocaleDateString(undefined, {
    month: 'short', day: 'numeric',
  })
}

/** "Overdue · 2d" / "Today" / "" — short, glanceable due chip. */
function taskDueLabel(iso: string | null): { text: string; tone: 'overdue' | 'today' | 'normal' } {
  if (!iso) return { text: '', tone: 'normal' }
  const due = new Date(`${iso}T00:00:00`)
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const days = Math.floor((due.getTime() - today.getTime()) / 86_400_000)
  if (days < 0)  return { text: `Overdue · ${-days}d`, tone: 'overdue' }
  if (days === 0) return { text: 'Today', tone: 'today' }
  if (days === 1) return { text: 'Tomorrow', tone: 'normal' }
  return { text: `In ${days}d`, tone: 'normal' }
}

/** "5m" / "2h" / "Mon" — relative-short for catch-up rows. */
function shortRelative(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60_000)
  if (mins < 1)   return 'now'
  if (mins < 60)  return `${mins}m`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h`
  const days = Math.floor(hours / 24)
  if (days < 7)   return `${days}d`
  return new Date(iso).toLocaleDateString(undefined, {
    weekday: 'short',
  })
}

/** Replace empty post bodies with a typed placeholder ("📷 Image").
 *  Keeps catch-up rows readable when the feed entry is media-only. */
function postSnippet(content: string | null, type: string): string {
  if (!content || !content.trim()) {
    switch (type) {
      case 'image':    return '📷 Image'
      case 'video':    return '🎬 Video'
      case 'file':     return '📄 File'
      case 'poll':     return '📊 Poll'
      case 'schedule': return '📅 Schedule'
      case 'bazaar':   return '🛍 Listing'
      case 'location': return '📍 Location'
      default:         return ''
    }
  }
  const flat = content.replace(/\s+/g, ' ').trim()
  return flat.length > 90 ? `${flat.slice(0, 90)}…` : flat
}

// ─── Page ──────────────────────────────────────────────────────────

const EMPTY: WelcomeBundle = {
  unread_notifications: 0,
  unread_conversations: 0,
  upcoming_events: [],
  tasks_due_today: [],
  followed_spaces_feed: [],
}

export default function WelcomePage() {
  useTitle('Welcome')
  const [bundle, setBundle] = useState<WelcomeBundle | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = async () => {
    try {
      const data = await api.get('/api/me/corner') as WelcomeBundle
      setBundle(data)
      setError(null)
    } catch (err: unknown) {
      setError((err as Error).message ?? 'Could not load.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void refresh()
    // Debounce the WS-driven refetch — the welcome surface is more
    // forgiving than the corner because we render fewer slices, but
    // the same firehose is in play (calendar / task / DM events).
    let timer: ReturnType<typeof setTimeout> | null = null
    const debounced = () => {
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => { void refresh() }, 200)
    }
    const events = [
      'notification.created', 'notification.read_changed',
      'dm.message',
      'calendar.event.created', 'calendar.event.updated',
      'calendar.event.deleted',
      'task.created', 'task.updated', 'task.deleted', 'task.completed',
    ]
    const offs = events.map(e => ws.on(e, debounced))
    return () => {
      offs.forEach(off => off())
      if (timer) clearTimeout(timer)
    }
  }, [])

  if (loading && !bundle) {
    return (
      <div class="sh-welcome">
        <div class="sh-welcome-skeleton">
          <CardSkeleton />
          <CardSkeleton />
        </div>
      </div>
    )
  }

  // Errors fall through to a calm card instead of an alarming alert
  // — the welcome page should never feel hostile.
  if (error && !bundle) {
    return (
      <div class="sh-welcome">
        <header class="sh-welcome-hero">
          <h1 class="sh-welcome-hero__greeting">Welcome back</h1>
          <p class="sh-welcome-hero__sub">
            {longDate(new Date())} · we couldn't load your day just now
          </p>
        </header>
      </div>
    )
  }

  const b = bundle ?? EMPTY
  const today = todaysEvents(b.upcoming_events)
  // When today's slot is empty but the calendar has something soon,
  // fall back to "Up next" — a household member with no plans today
  // shouldn't open the app to a Catch-Up-only page when there's a
  // dinner reservation tomorrow.
  const upNext = today.length === 0 ? nextEvents(b.upcoming_events) : []
  const tasks = b.tasks_due_today
  const catchUp = b.followed_spaces_feed.slice(0, 3)
  const allClear =
    today.length === 0
    && upNext.length === 0
    && tasks.length === 0
    && catchUp.length === 0
    && b.unread_notifications === 0
    && b.unread_conversations === 0

  // Hero sub-line — describes the day in three words instead of a
  // sterile count.  Keeps the welcome warm even when the day is busy.
  const greetee = firstName(currentUser.value?.display_name)
  const heroGreeting = greetee
    ? `${timeOfDayGreeting()}, ${greetee}`
    : timeOfDayGreeting()
  const heroSub = allClear
    ? `${longDate(new Date())} · all clear`
    : `${longDate(new Date())} · ${dayShape(today, tasks, upNext, b)}`

  return (
    <div class="sh-welcome">
      <header class="sh-welcome-hero">
        <h1 class="sh-welcome-hero__greeting">{heroGreeting}</h1>
        <p class="sh-welcome-hero__sub">{heroSub}</p>
      </header>

      {allClear ? (
        <AllClearCard />
      ) : (
        <div class="sh-welcome-stack">
          {today.length > 0 && (
            <TodayCard events={today} />
          )}
          {today.length === 0 && upNext.length > 0 && (
            <UpNextCard events={upNext} />
          )}
          {tasks.length > 0 && (
            <PendingCard tasks={tasks} />
          )}
          <CatchUpCard
            posts={catchUp}
            unreadNotifications={b.unread_notifications}
            unreadConversations={b.unread_conversations}
          />
        </div>
      )}
    </div>
  )
}

/** Compose the hero sub-line — "2 events · 3 tasks" style.  Punchy
 *  enough that the user can decide in 1s whether they need to dig
 *  in.  Cascades through several signals so the line is always
 *  specific:
 *
 *    today's events + tasks  →  "2 events · 3 tasks"
 *    nothing today, future   →  "nothing today · next up tomorrow"
 *    nothing scheduled, but
 *      messages / alerts     →  "13 alerts to read"
 *    truly nothing           →  caller already swapped to all-clear
 */
function dayShape(
  events: WelcomeEvent[],
  tasks: WelcomeTask[],
  upNext: WelcomeEvent[],
  b: WelcomeBundle,
): string {
  const parts: string[] = []
  if (events.length > 0) {
    parts.push(events.length === 1 ? '1 event' : `${events.length} events`)
  }
  if (tasks.length > 0) {
    parts.push(tasks.length === 1 ? '1 task' : `${tasks.length} tasks`)
  }
  if (parts.length > 0) return parts.join(' · ')

  // No today-scoped activity. Lead with the calendar fallback.
  if (upNext.length > 0) {
    const next = upNext[0]
    return `nothing today · next up ${dayLabel(next.start).toLowerCase()}`
  }

  // No calendar either.  Inbox-only days deserve their own copy so
  // the hero doesn't lie about activity that exists in catch-up.
  const inboxParts: string[] = []
  if (b.unread_conversations > 0) {
    inboxParts.push(b.unread_conversations === 1
      ? '1 message'
      : `${b.unread_conversations} messages`)
  }
  if (b.unread_notifications > 0) {
    inboxParts.push(b.unread_notifications === 1
      ? '1 alert'
      : `${b.unread_notifications} alerts`)
  }
  if (inboxParts.length > 0) return `${inboxParts.join(' · ')} to read`

  return 'a few things waiting'
}

// ─── Cards ─────────────────────────────────────────────────────────

function TodayCard({ events }: { events: WelcomeEvent[] }) {
  return (
    <a class="sh-welcome-card" href="/calendar">
      <h2 class="sh-welcome-card__title">
        <span aria-hidden="true">📅</span> Today
      </h2>
      <ul class="sh-welcome-card__list">
        {events.map(e => (
          <li key={e.id} class="sh-welcome-card__row">
            <time class="sh-welcome-card__time">
              {e.all_day ? 'All day' : eventTime(e)}
            </time>
            <span class="sh-welcome-card__line">{e.summary}</span>
          </li>
        ))}
      </ul>
      <span class="sh-welcome-card__more">Open calendar →</span>
    </a>
  )
}

/** Calendar fallback when today is empty — surfaces the next 1-2
 *  events with a "Tomorrow / Mon / May 12" date prefix so the user
 *  knows when each one lands without thinking. */
function UpNextCard({ events }: { events: WelcomeEvent[] }) {
  return (
    <a class="sh-welcome-card" href="/calendar">
      <h2 class="sh-welcome-card__title">
        <span aria-hidden="true">🗓</span> Up next
      </h2>
      <ul class="sh-welcome-card__list">
        {events.map(e => (
          <li key={e.id} class="sh-welcome-card__row">
            <time class="sh-welcome-card__time sh-welcome-card__time--day">
              {dayLabel(e.start)}
            </time>
            <span class="sh-welcome-card__line">
              {e.summary}
              {!e.all_day && (
                <span class="sh-welcome-card__when sh-muted">
                  {' · '}{eventTime(e)}
                </span>
              )}
            </span>
          </li>
        ))}
      </ul>
      <span class="sh-welcome-card__more">Open calendar →</span>
    </a>
  )
}

function PendingCard({ tasks }: { tasks: WelcomeTask[] }) {
  // Render at most five rows here — anything beyond that turns the
  // welcome into a task page; the "Open tasks" footer carries them
  // through to /organize for the full list.
  const visible = tasks.slice(0, 5)
  const overflow = tasks.length - visible.length
  return (
    <a class="sh-welcome-card" href="/organize">
      <h2 class="sh-welcome-card__title">
        <span aria-hidden="true">✅</span> Pending
      </h2>
      <ul class="sh-welcome-card__list">
        {visible.map(t => {
          const due = taskDueLabel(t.due_date)
          return (
            <li key={t.id} class="sh-welcome-card__row">
              <span class="sh-welcome-card__bullet" aria-hidden="true">⬜</span>
              <span class="sh-welcome-card__line">{t.title}</span>
              {due.text && (
                <span class={`sh-welcome-card__chip sh-welcome-card__chip--${due.tone}`}>
                  {due.text}
                </span>
              )}
            </li>
          )
        })}
        {overflow > 0 && (
          <li class="sh-welcome-card__row sh-welcome-card__row--more">
            +{overflow} more
          </li>
        )}
      </ul>
      <span class="sh-welcome-card__more">Open tasks →</span>
    </a>
  )
}

function CatchUpCard({
  posts, unreadNotifications, unreadConversations,
}: {
  posts: WelcomeFollowedPost[]
  unreadNotifications: number
  unreadConversations: number
}) {
  const hasAnything = posts.length > 0
    || unreadNotifications > 0
    || unreadConversations > 0
  if (!hasAnything) return null
  return (
    <section class="sh-welcome-card sh-welcome-card--catchup">
      <h2 class="sh-welcome-card__title">
        <span aria-hidden="true">✨</span> Catch up
      </h2>
      {/* Inbox chips first — DMs / notifications are time-sensitive and
       *  belong above slower-moving space activity. */}
      <div class="sh-welcome-chips">
        {unreadConversations > 0 && (
          <a class="sh-welcome-chip" href="/dms">
            <span aria-hidden="true">💬</span>
            <strong>{unreadConversations}</strong>
            <span>{unreadConversations === 1 ? 'message' : 'messages'}</span>
          </a>
        )}
        {unreadNotifications > 0 && (
          <a class="sh-welcome-chip" href="/notifications">
            <span aria-hidden="true">🔔</span>
            <strong>{unreadNotifications}</strong>
            <span>{unreadNotifications === 1 ? 'alert' : 'alerts'}</span>
          </a>
        )}
      </div>
      {posts.length > 0 && (
        <ul class="sh-welcome-card__list">
          {posts.map(p => (
            <li key={p.post_id}>
              <a class="sh-welcome-catchup-row" href={`/spaces/${p.space_id}`}>
                <span class="sh-welcome-catchup-emoji" aria-hidden="true">
                  {p.space_emoji || '🪐'}
                </span>
                <span class="sh-welcome-catchup-body">
                  <span class="sh-welcome-catchup-meta">
                    <Avatar
                      name={p.author}
                      src={null}
                      size={18}
                    />
                    <strong>{p.author}</strong>
                    <span class="sh-muted">in {p.space_name}</span>
                  </span>
                  <span class="sh-welcome-catchup-snippet">
                    {postSnippet(p.content, p.type)}
                  </span>
                </span>
                <time class="sh-welcome-catchup-when sh-muted">
                  {shortRelative(p.created_at)}
                </time>
              </a>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function AllClearCard() {
  return (
    <div class="sh-welcome-allclear">
      <span class="sh-welcome-allclear__sun" aria-hidden="true">☀️</span>
      <h2 class="sh-welcome-allclear__title">All clear</h2>
      <p class="sh-welcome-allclear__sub sh-muted">
        Nothing on your plate today. Enjoy the quiet.
      </p>
    </div>
  )
}
