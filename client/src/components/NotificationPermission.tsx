/**
 * NotificationPermission — push permission prompt (§23.33).
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Button } from './Button'

const dismissed = signal(false)
const granted = signal(
  typeof Notification !== 'undefined' ? Notification.permission === 'granted' : false
)
const subscribing = signal(false)

async function registerPushSubscription(): Promise<void> {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return
  try {
    // Relative URL — resolves against ``document.baseURI`` so the SW
    // is loaded from the ingress prefix (``/api/hassio_ingress/<token>/sw.js``)
    // when behind HA Supervisor ingress, and from ``/sw.js`` otherwise.
    // The default scope (the script's directory) follows along: the
    // worker controls the add-on's URL space without leaking onto HA
    // Core paths.
    const registration = await navigator.serviceWorker.register('sw.js')
    await navigator.serviceWorker.ready
    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      // Cast to BufferSource — lib.dom's type requires an ArrayBuffer,
      // while our Uint8Array<ArrayBufferLike> is narrower in TS 5.7+.
      applicationServerKey: (await getVapidKey()) as BufferSource | undefined,
    })
    await api.post('/api/push/subscribe', subscription.toJSON())
  } catch {
    // Push subscription failed — degrade silently.
  }
}

async function getVapidKey(): Promise<Uint8Array | undefined> {
  try {
    const resp = await api.get('/api/push/vapid-key') as { public_key: string }
    const raw = atob(resp.public_key.replace(/-/g, '+').replace(/_/g, '/'))
    const bytes = new Uint8Array(raw.length)
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i)
    return bytes
  } catch {
    return undefined
  }
}

export function NotificationPermissionBanner() {
  if (granted.value || dismissed.value) return null
  if (typeof Notification === 'undefined') return null
  if (Notification.permission === 'denied') return null

  const request = async () => {
    subscribing.value = true
    const result = await Notification.requestPermission()
    granted.value = result === 'granted'
    if (result === 'granted') {
      await registerPushSubscription()
    }
    subscribing.value = false
    dismissed.value = true
  }

  return (
    <div class="sh-permission-banner" role="alert">
      <span>Enable push notifications to stay updated.</span>
      <Button onClick={request} loading={subscribing.value}>Enable</Button>
      <button class="sh-link" onClick={() => dismissed.value = true}>Not now</button>
    </div>
  )
}
