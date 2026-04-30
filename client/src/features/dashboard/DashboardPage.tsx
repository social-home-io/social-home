/**
 * DashboardPage — "My Corner" (§23).
 *
 * One round-trip to ``GET /api/me/corner`` populates every widget.
 * Live WS events debounce-refetch the whole bundle (cheap because the
 * server does 1 SQL query per slice — ~10 ms total on a warm cache).
 */
import type { ComponentChildren } from 'preact'
import { useTitle } from '@/store/pageTitle'
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { ws } from '@/ws'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { CardSkeleton } from '@/components/SkeletonScreen'
import { FollowedSpacesPicker } from '@/components/FollowedSpacesPicker'
import { LocationMap } from '@/components/LocationMap'
import NetworkMap from './NetworkMap'

interface CornerEvent {
  id: string
  summary: string
  start: string
  end: string
  all_day: boolean
}

interface CornerPresence {
  user_id: string
  username: string
  display_name: string
  picture_url: string | null
  state: string
  zone_name: string | null
  latitude?: number | null
  longitude?: number | null
  gps_accuracy_m?: number | null
}

interface CornerTask {
  id: string
  list_id: string
  title: string
  status: 'todo' | 'in_progress' | 'done'
  due_date: string | null
}

interface BazaarCornerSummary {
  active_listings: number
  pending_offers: number
  ending_soon: number
}

interface FollowedSpacePost {
  post_id: string
  space_id: string
  space_name: string
  space_emoji: string | null
  author: string
  type: string
  content: string | null
  created_at: string
}

interface CornerBundle {
  unread_notifications: number
  unread_conversations: number
  upcoming_events: CornerEvent[]
  presence: CornerPresence[]
  tasks_due_today: CornerTask[]
  bazaar: BazaarCornerSummary
  followed_space_ids: string[]
  followed_spaces_feed: FollowedSpacePost[]
}

const EMPTY_BUNDLE: CornerBundle = {
  unread_notifications: 0,
  unread_conversations: 0,
  upcoming_events: [],
  presence: [],
  tasks_due_today: [],
  bazaar: { active_listings: 0, pending_offers: 0, ending_soon: 0 },
  followed_space_ids: [],
  followed_spaces_feed: [],
}

function presenceDotClass(state: string): string {
  switch (state) {
    case 'home':     return 'sh-dot sh-dot--home'
    case 'away':     return 'sh-dot sh-dot--away'
    case 'zone':     return 'sh-dot sh-dot--home'
    case 'not_home': return 'sh-dot sh-dot--not-home'
    default:         return 'sh-dot sh-dot--unknown'
  }
}

function formatEventWhen(e: CornerEvent): string {
  const d = new Date(e.start)
  const today = new Date()
  const sameDay = d.toDateString() === today.toDateString()
  const when = d.toLocaleDateString(undefined, {
    weekday: 'short', month: 'short', day: 'numeric',
  })
  if (e.all_day) return `${when} · All day`
  const time = d.toLocaleTimeString(undefined, {
    hour: '2-digit', minute: '2-digit',
  })
  return sameDay ? `Today · ${time}` : `${when} · ${time}`
}

function formatRelativeShort(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60_000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days}d`
  return new Date(iso).toLocaleDateString(undefined, {
    month: 'short', day: 'numeric',
  })
}

function contentSnippet(content: string | null, type: string): string {
  if (!content || !content.trim()) {
    switch (type) {
      case 'image':    return '📷 Image'
      case 'video':    return '🎬 Video'
      case 'file':     return '📄 File'
      case 'poll':     return '📊 Poll'
      case 'schedule': return '📅 Schedule'
      case 'bazaar':   return '🛍 Listing'
      default:         return '(no content)'
    }
  }
  const clean = content.replace(/\s+/g, ' ').trim()
  return clean.length > 120 ? `${clean.slice(0, 120)}…` : clean
}

function formatTaskDue(iso: string | null): string {
  if (!iso) return ''
  const due = new Date(`${iso}T00:00:00`)
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const diffDays = Math.floor(
    (due.getTime() - today.getTime()) / 86_400_000,
  )
  if (diffDays < 0) return `Overdue by ${-diffDays}d`
  if (diffDays === 0) return 'Due today'
  if (diffDays === 1) return 'Due tomorrow'
  return `Due in ${diffDays}d`
}

export default function DashboardPage() {
  useTitle('My Corner')
  const [bundle, setBundle] = useState<CornerBundle | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [pickerOpen, setPickerOpen] = useState(false)

  const refresh = async () => {
    try {
      const data = await api.get('/api/me/corner') as CornerBundle
      setBundle(data)
      setError(null)
    } catch (err: unknown) {
      setError((err as Error).message ?? 'Could not load corner.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void refresh()
    // Debounced refetch — cluster bursts of events (e.g. five
    // task.updated in a second) into one round-trip.
    let timer: ReturnType<typeof setTimeout> | null = null
    const debouncedRefresh = () => {
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => { void refresh() }, 200)
    }
    const relevant = [
      'notification.created', 'notification.read_changed',
      'dm.message',
      'calendar.event.created', 'calendar.event.updated',
      'calendar.event.deleted',
      'presence.updated',
      'task.created', 'task.updated', 'task.deleted',
      'task.completed', 'task.assigned',
      'bazaar.bid_placed', 'bazaar.listing_created',
      'bazaar.listing_closed', 'bazaar.offer_accepted',
    ]
    const offs = relevant.map(t => ws.on(t, debouncedRefresh))
    return () => {
      offs.forEach(off => off())
      if (timer) clearTimeout(timer)
    }
  }, [])

  if (loading && !bundle) {
    return (
      <div class="sh-dashboard">
        <div class="sh-dashboard-grid">
          {Array.from({ length: 4 }, (_, i) => <CardSkeleton key={i} />)}
        </div>
      </div>
    )
  }

  if (error && !bundle) {
    return (
      <div class="sh-dashboard">
        <div class="sh-empty-state">
          <div style={{ fontSize: '2rem' }}>⚠️</div>
          <h3>Couldn't load your corner</h3>
          <p class="sh-muted">{error}</p>
          <Button onClick={() => { setLoading(true); void refresh() }}>
            Retry
          </Button>
        </div>
      </div>
    )
  }

  const b = bundle ?? EMPTY_BUNDLE

  return (
    <div class="sh-dashboard">
      <div class="sh-dashboard-grid">
        <StatWidget
          icon="🔔" label="Notifications"
          value={b.unread_notifications} unit="unread"
          href="/notifications" />
        <StatWidget
          icon="💬" label="Messages"
          value={b.unread_conversations} unit="unread"
          href="/dms" />

        <Widget title="📅 Upcoming events" href="/calendar"
                empty={b.upcoming_events.length === 0}
                emptyIcon="📅" emptyText="No upcoming events">
          {b.upcoming_events.map(e => (
            <div key={e.id} class="sh-widget-event">
              <strong>{e.summary}</strong>
              <time class="sh-muted">{formatEventWhen(e)}</time>
            </div>
          ))}
        </Widget>

        <Widget title="✅ Tasks due" href="/tasks"
                empty={b.tasks_due_today.length === 0}
                emptyIcon="🎉" emptyText="You're all caught up!">
          {b.tasks_due_today.map(t => (
            <div key={t.id} class={`sh-widget-task sh-widget-task--${t.status}`}>
              <span class="sh-widget-task-title">{t.title}</span>
              <span class="sh-widget-task-due">
                {formatTaskDue(t.due_date)}
              </span>
            </div>
          ))}
        </Widget>

        <Widget title="🏠 Who's home" href="/presence"
                empty={b.presence.length === 0}
                emptyIcon="👋" emptyText="No presence data yet">
          <div class="sh-presence-overview">
            {b.presence.map(p => (
              <div key={p.user_id} class="sh-presence-mini">
                <span class={presenceDotClass(p.state)} />
                <Avatar name={p.display_name} src={p.picture_url} size={28} />
                <span>{p.display_name}</span>
                <span class="sh-muted">{p.zone_name || p.state}</span>
              </div>
            ))}
          </div>
          {b.presence.some(
            (p) => typeof p.latitude === 'number'
              && typeof p.longitude === 'number',
          ) && (
            <LocationMap
              markers={b.presence
                .filter((p) =>
                  typeof p.latitude === 'number'
                  && typeof p.longitude === 'number',
                )
                .map((p) => ({
                  id: p.user_id,
                  lat: p.latitude as number,
                  lon: p.longitude as number,
                  accuracy_m: p.gps_accuracy_m ?? null,
                  label: p.display_name,
                  sub_label: p.zone_name,
                  avatar_url: p.picture_url,
                  state: p.state,
                }))}
              height={220}
              emptyLabel="No one is sharing GPS."
            />
          )}
        </Widget>

        <Widget title="🛍 Bazaar" href="/bazaar"
                empty={
                  b.bazaar.active_listings === 0
                  && b.bazaar.pending_offers === 0
                }
                emptyIcon="🛍️"
                emptyText="You have no active listings.">
          <div class="sh-corner-bazaar">
            <div class="sh-corner-bazaar-stat">
              <span class="sh-corner-bazaar-value">
                {b.bazaar.active_listings}
              </span>
              <span class="sh-muted">Active</span>
            </div>
            <div class="sh-corner-bazaar-stat">
              <span class="sh-corner-bazaar-value">
                {b.bazaar.pending_offers}
              </span>
              <span class="sh-muted">Offers to review</span>
            </div>
            {b.bazaar.ending_soon > 0 && (
              <div class="sh-corner-bazaar-stat sh-corner-bazaar-stat--warn">
                <span class="sh-corner-bazaar-value">
                  {b.bazaar.ending_soon}
                </span>
                <span class="sh-muted">Ending &lt; 24h</span>
              </div>
            )}
          </div>
        </Widget>

        <FollowedSpacesWidget
          posts={b.followed_spaces_feed}
          followedCount={b.followed_space_ids.length}
          onManage={() => setPickerOpen(true)} />

        <Widget title="⚡ Quick actions">
          <div class="sh-quick-actions">
            <a href="/feed" class="sh-btn sh-btn--secondary">Feed</a>
            <a href="/dms" class="sh-btn sh-btn--secondary">Messages</a>
            <a href="/calendar" class="sh-btn sh-btn--secondary">Calendar</a>
            <a href="/shopping" class="sh-btn sh-btn--secondary">Shopping</a>
            <a href="/tasks" class="sh-btn sh-btn--secondary">Tasks</a>
            <a href="/bazaar" class="sh-btn sh-btn--secondary">Bazaar</a>
          </div>
        </Widget>

        <div class="sh-widget sh-widget--wide sh-widget--networkmap">
          <NetworkMap />
        </div>
      </div>
      <FollowedSpacesPicker open={pickerOpen}
                            onClose={() => setPickerOpen(false)}
                            onChanged={() => { void refresh() }} />
    </div>
  )
}

function FollowedSpacesWidget({
  posts, followedCount, onManage,
}: {
  posts: FollowedSpacePost[]
  followedCount: number
  onManage: () => void
}) {
  return (
    <div class="sh-widget sh-widget--wide">
      <div class="sh-widget-header">
        <h3>🛰 Spaces you follow</h3>
        <button type="button" class="sh-widget-link sh-link-button"
                onClick={onManage}
                aria-label="Manage followed spaces">
          Manage →
        </button>
      </div>
      {followedCount === 0 ? (
        <div class="sh-widget-empty">
          <span class="sh-widget-empty-icon" aria-hidden="true">🛰</span>
          <span class="sh-muted">Pick spaces to see their posts here.</span>
          <Button onClick={onManage}>Choose spaces</Button>
        </div>
      ) : posts.length === 0 ? (
        <div class="sh-widget-empty">
          <span class="sh-widget-empty-icon" aria-hidden="true">✨</span>
          <span class="sh-muted">
            No new posts in the spaces you follow.
          </span>
        </div>
      ) : (
        <div class="sh-followed-feed">
          {posts.map(p => (
            <a key={p.post_id}
               class="sh-followed-row"
               href={`/spaces/${p.space_id}`}>
              <span class="sh-space-chip"
                    title={p.space_name}>
                <span class="sh-space-chip-emoji" aria-hidden="true">
                  {p.space_emoji || '🪐'}
                </span>
                <span class="sh-space-chip-name">{p.space_name}</span>
              </span>
              <div class="sh-followed-body">
                <span class="sh-followed-author">{p.author}</span>
                <span class="sh-followed-snippet">
                  {contentSnippet(p.content, p.type)}
                </span>
              </div>
              <time class="sh-muted sh-followed-when"
                    title={new Date(p.created_at).toLocaleString()}>
                {formatRelativeShort(p.created_at)}
              </time>
            </a>
          ))}
        </div>
      )}
    </div>
  )
}

function StatWidget({
  icon, label, value, unit, href,
}: {
  icon: string
  label: string
  value: number
  unit: string
  href?: string
}) {
  const body = (
    <>
      <div class="sh-stat-widget-icon" aria-hidden="true">{icon}</div>
      <div class="sh-stat-widget-body">
        <span class="sh-widget-count">{value}</span>
        <span class="sh-muted">{unit}</span>
        <h3>{label}</h3>
      </div>
    </>
  )
  if (href) {
    return (
      <a class="sh-widget sh-stat-widget sh-widget--link" href={href}>
        {body}
      </a>
    )
  }
  return <div class="sh-widget sh-stat-widget">{body}</div>
}

function Widget({
  title, href, empty, emptyIcon, emptyText, children,
}: {
  title: string
  href?: string
  empty?: boolean
  emptyIcon?: string
  emptyText?: string
  children?: ComponentChildren
}) {
  return (
    <div class="sh-widget sh-widget--wide">
      <div class="sh-widget-header">
        <h3>{title}</h3>
        {href && (
          <a class="sh-widget-link sh-muted" href={href}>View all →</a>
        )}
      </div>
      {empty ? (
        <div class="sh-widget-empty">
          <span class="sh-widget-empty-icon" aria-hidden="true">
            {emptyIcon ?? '—'}
          </span>
          <span class="sh-muted">{emptyText ?? 'Nothing here yet.'}</span>
        </div>
      ) : children}
    </div>
  )
}
