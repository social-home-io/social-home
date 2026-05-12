/* Service worker for push notifications (§23.33).
 *
 * URL resolution under HA Supervisor ingress
 * ------------------------------------------
 * The worker is registered with a relative URL so its scope tracks the
 * document base. Under ingress that is ``/api/hassio_ingress/<token>/``;
 * outside ingress it is ``/``. Any URL we open or fetch from inside the
 * worker is resolved against ``registration.scope`` so notifications open
 * the add-on, not HA Core.
 */
function resolveInScope(target) {
  // Strip a leading slash so server-supplied paths like ``/feed`` resolve
  // **inside** the worker's scope rather than escaping to the origin root.
  const rel = String(target || '').replace(/^\/+/, '')
  return new URL(rel, self.registration.scope).href
}

self.addEventListener('push', (event) => {
  const data = event.data ? event.data.json() : {}
  const title = data.title || 'Social Home'
  const options = {
    body: data.body || '',
    data: { url: resolveInScope(data.url || '') },
  }
  event.waitUntil(self.registration.showNotification(title, options))
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const url = event.notification.data?.url || self.registration.scope
  event.waitUntil(clients.openWindow(url))
})
