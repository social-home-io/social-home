/* Shared fetch wrapper for the GFS admin panel.
 *
 * Mirrors the inline ``api()`` from the previous hand-written
 * ``admin_ui/index.html`` so the call sites port over verbatim.
 * Surfaces a typed ``UnauthorizedError`` so the App component can
 * route back to the login gate without inspecting status codes.
 *
 * Path handling
 * -------------
 * Caller-supplied paths look absolute (``/admin/api/overview``) so the
 * panel files read naturally. ``fetch`` is fed the **relative** form
 * (``admin/api/overview``) so the browser resolves it against
 * ``<base href>`` rather than the document origin. The two shapes
 * resolve to the same URL on the standalone GFS deploy, and the
 * relative form is what makes the UI portable to a path-prefixed
 * reverse proxy / future ingress front.
 */

export class UnauthorizedError extends Error {}


export async function api<T = unknown>(
  method: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE',
  path: string,
  body?: unknown,
): Promise<T> {
  const opts: RequestInit = {
    method,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
  }
  if (body !== undefined) opts.body = JSON.stringify(body)
  const resp = await fetch(path.replace(/^\/+/, ''), opts)
  if (resp.status === 401) {
    throw new UnauthorizedError('Session expired')
  }
  const text = await resp.text()
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`
    try {
      const parsed = JSON.parse(text) as { detail?: string }
      if (parsed?.detail) msg = parsed.detail
    } catch { /* ignore */ }
    throw new Error(msg)
  }
  return (text ? JSON.parse(text) : {}) as T
}


export type Status = 'active' | 'pending' | 'banned'


export function pillClass(status: string): string {
  if (status === 'active' || status === 'pending' || status === 'banned') {
    return `pill ${status}`
  }
  return 'pill pending'
}


export function fmtTime(unix: number): string {
  return new Date(unix * 1000).toLocaleString()
}
