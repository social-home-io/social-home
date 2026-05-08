/**
 * NotificationsPage — notification centre (§23.3).
 */
import { useEffect } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { NotificationListSkeleton } from '@/components/Skeleton'
import { showToast } from '@/components/Toast'
import type { Notification } from '@/types'


/** Friendly relative timestamp for the notifications row. Mirrors the
 *  Pages-index formatter — same rungs, same shape — so a viewer scanning
 *  both surfaces never has to context-switch between two time languages. */
function relativeNotifTime(iso: string): string {
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  const now = Date.now()
  const diff = now - t
  const min = Math.floor(diff / 60_000)
  if (min < 1) return 'just now'
  if (min < 60) return `${min} min ago`
  const hr = Math.floor(min / 60)
  const sameDay = new Date(t).toDateString() === new Date(now).toDateString()
  if (sameDay) return `${hr}h ago`
  const yesterday = new Date(now)
  yesterday.setDate(yesterday.getDate() - 1)
  if (new Date(t).toDateString() === yesterday.toDateString()) return 'yesterday'
  if (diff < 7 * 86_400_000) {
    return `${Math.floor(diff / 86_400_000)} days ago`
  }
  return new Date(t).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year:
      new Date(t).getFullYear() === new Date(now).getFullYear()
        ? undefined
        : 'numeric',
  })
}

const notifications = signal<Notification[]>([])
const loading = signal(true)

export default function NotificationsPage() {
  useTitle('Notifications')
  useEffect(() => {
    api.get('/api/notifications?limit=50').then(data => {
      notifications.value = data
      loading.value = false
    })
  }, [])

  const markAllRead = async () => {
    try {
      await api.post('/api/notifications/read-all')
      notifications.value = notifications.value.map(n => ({
        ...n, read_at: n.read_at || new Date().toISOString(),
      }))
    } catch (err: unknown) {
      showToast(`Mark-all-read failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  const markRead = async (id: string) => {
    try {
      await api.post(`/api/notifications/${id}/read`)
      notifications.value = notifications.value.map(n =>
        n.id === id ? { ...n, read_at: new Date().toISOString() } : n
      )
    } catch (err: unknown) {
      showToast(`Mark-read failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  if (loading.value) return <NotificationListSkeleton />

  return (
    <div class="sh-notifications-page">
      <div class="sh-page-header">
        <Button variant="secondary" onClick={markAllRead}>Mark all read</Button>
      </div>
      {notifications.value.length === 0 && (
        <div class="sh-empty-state">
          <div aria-hidden="true">🔔</div>
          <h3>You're all caught up</h3>
          <p>
            New notifications will land here when someone reacts to a post,
            invites you to an event, or mentions you in a thread.
          </p>
        </div>
      )}
      {notifications.value.map(n => (
        <div key={n.id}
          class={`sh-notif-row ${n.read_at ? '' : 'sh-notif-row--unread'}`}
          onClick={() => !n.read_at && markRead(n.id)}>
          <div class="sh-notif-icon">{n.read_at ? '○' : '●'}</div>
          <div class="sh-notif-content">
            <div class="sh-notif-title">{n.title}</div>
            {n.body && <div class="sh-notif-body">{n.body}</div>}
            <time
              class="sh-notif-time"
              dateTime={n.created_at}
              title={new Date(n.created_at).toLocaleString()}
            >
              {relativeNotifTime(n.created_at)}
            </time>
          </div>
          {n.link_url && <a href={n.link_url} class="sh-notif-link">→</a>}
        </div>
      ))}
    </div>
  )
}
