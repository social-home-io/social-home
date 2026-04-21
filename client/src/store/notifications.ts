/**
 * Notifications store — driven by `notification.new` and
 * `notification.unread_count` WS frames (§21).
 *
 * The bell badge in the top bar reads :data:`unreadCount`; the
 * notifications page reads :data:`recent`. Both update without
 * polling thanks to the WS subscription wired below.
 */
import { signal } from '@preact/signals'
import { ws } from '@/ws'

export interface NotificationLite {
  notification_id: string
  notif_type:      string
  title:           string
  occurred_at?:    string
}

export const recent      = signal<NotificationLite[]>([])
export const unreadCount = signal<number>(0)

export function wireNotificationsWs(): void {
  ws.on('notification.new', (e) => {
    const n = e.data as unknown as NotificationLite
    recent.value = [n, ...recent.value].slice(0, 50)
    unreadCount.value = unreadCount.value + 1
  })
  ws.on('notification.unread_count', (e) => {
    const c = (e.data as { unread_count: number }).unread_count
    if (typeof c === 'number') unreadCount.value = c
  })
}
