/**
 * NotificationsPage — notification centre (§23.3).
 */
import { useEffect } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import type { Notification } from '@/types'

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

  if (loading.value) return <Spinner />

  return (
    <div class="sh-notifications-page">
      <div class="sh-page-header">
        <Button variant="secondary" onClick={markAllRead}>Mark all read</Button>
      </div>
      {notifications.value.length === 0 && <p class="sh-muted">No notifications yet.</p>}
      {notifications.value.map(n => (
        <div key={n.id}
          class={`sh-notif-row ${n.read_at ? '' : 'sh-notif-row--unread'}`}
          onClick={() => !n.read_at && markRead(n.id)}>
          <div class="sh-notif-icon">{n.read_at ? '○' : '●'}</div>
          <div class="sh-notif-content">
            <div class="sh-notif-title">{n.title}</div>
            {n.body && <div class="sh-notif-body">{n.body}</div>}
            <time class="sh-notif-time">{new Date(n.created_at).toLocaleString()}</time>
          </div>
          {n.link_url && <a href={n.link_url} class="sh-notif-link">→</a>}
        </div>
      ))}
    </div>
  )
}
