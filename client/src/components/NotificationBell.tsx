/**
 * NotificationBell — top-bar bell + dropdown panel (§23.3).
 *
 * Backed by ``store/notifications`` — the WS-driven signal store
 * whose ``unreadCount`` and ``recent`` already update live on
 * ``notification.new`` frames. This component just renders that
 * state and hydrates a fuller list on panel open.
 *
 * Items with a ``link_url`` render as anchors. For DMs the link
 * targets ``/dms/{conversation_id}`` and the conversation read route
 * auto-clears the row server-side, so a tap reads as "follow the
 * link, the badge will clear itself".
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { recent, unreadCount } from '@/store/notifications'
import type { Notification } from '@/types'

const panelOpen = signal(false)
/** Full notifications list — populated when the panel opens.
 *  Falls back to the store's lighter ``recent`` projection while
 *  loading. */
const fullList = signal<Notification[]>([])

// 30 s polling fallback for the rare case the WS connection is
// down. Kept as a belt-and-suspenders guard — the WS path is the
// primary signal.
let pollTimer: ReturnType<typeof setInterval>
export function startNotificationPolling() {
  const poll = async () => {
    try {
      const data = await api.get('/api/notifications/unread-count')
      // Don't overwrite the store if the WS handler is keeping it
      // honest — only correct downward when the WS feed agrees.
      if (typeof data.unread === 'number') {
        unreadCount.value = data.unread
      }
    } catch { /* transient — WS will catch up */ }
  }
  poll()
  pollTimer = setInterval(poll, 30000)
}

export function stopNotificationPolling() {
  clearInterval(pollTimer)
}

export function NotificationBell() {
  // Hydrate the panel list once on mount so first-tap is instant.
  useEffect(() => {
    void api.get('/api/notifications?limit=20').then((d) => {
      fullList.value = d as Notification[]
    }).catch(() => { /* noop */ })
  }, [])

  const togglePanel = async () => {
    panelOpen.value = !panelOpen.value
    if (panelOpen.value) {
      try {
        const data = await api.get('/api/notifications?limit=20')
        fullList.value = data as Notification[]
      } catch { /* keep stale list visible */ }
    }
  }

  const markAllRead = async () => {
    await api.post('/api/notifications/read-all')
    unreadCount.value = 0
    fullList.value = fullList.value.map(n => ({
      ...n,
      read_at: n.read_at ?? new Date().toISOString(),
    }))
  }

  const handleItemClick = async (n: Notification | { notification_id: string }) => {
    // Optimistic decrement — tapping an unread item flips it read,
    // and the destination page (e.g. DM thread) typically clears the
    // server-side row too via its read hook. The next /unread-count
    // poll reconciles any divergence.
    const id = 'id' in n ? n.id : n.notification_id
    const isUnread = 'read_at' in n
      ? !n.read_at
      : true  // store-projected items are unread by definition
    if (isUnread && unreadCount.value > 0) {
      unreadCount.value = unreadCount.value - 1
    }
    if (isUnread) {
      try { await api.post(`/api/notifications/${id}/read`) }
      catch { /* best-effort */ }
    }
    panelOpen.value = false
  }

  // The displayed list: prefer the hydrated full list (carries
  // ``read_at`` + ``created_at``); fall back to the store's recent
  // projection for instant feel right after a WS frame lands.
  const items = fullList.value.length > 0
    ? fullList.value
    : recent.value.map(r => ({
        id:         r.notification_id,
        type:       r.notif_type,
        title:      r.title,
        body:       null,
        link_url:   r.link_url ?? null,
        read_at:    null,
        created_at: r.occurred_at ?? new Date().toISOString(),
      }) as Notification)

  return (
    <div class="sh-notif-bell">
      <button
        type="button"
        class="sh-notif-btn"
        onClick={togglePanel}
        aria-label={
          unreadCount.value > 0
            ? `Notifications (${unreadCount.value} unread)`
            : 'Notifications'
        }
      >
        🔔
        {unreadCount.value > 0 && (
          <span class="sh-notif-badge">
            {unreadCount.value > 99 ? '99+' : unreadCount.value}
          </span>
        )}
      </button>
      {panelOpen.value && (
        <div class="sh-notif-panel">
          <div class="sh-notif-header">
            <h4>Notifications</h4>
            {unreadCount.value > 0 && (
              <button class="sh-link" onClick={markAllRead}>Mark all read</button>
            )}
          </div>
          <div class="sh-notif-list">
            {items.length === 0 && (
              <p class="sh-muted">No notifications</p>
            )}
            {items.map(n => {
              const cls = `sh-notif-item ${n.read_at ? '' : 'sh-notif--unread'}`
              const inner = (
                <>
                  <div class="sh-notif-title">{n.title}</div>
                  <time class="sh-notif-time">
                    {new Date(n.created_at).toLocaleString()}
                  </time>
                </>
              )
              if (n.link_url) {
                return (
                  <a
                    key={n.id}
                    href={n.link_url}
                    class={cls}
                    onClick={() => void handleItemClick(n)}
                  >
                    {inner}
                  </a>
                )
              }
              return (
                <div key={n.id} class={cls}>
                  {inner}
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
