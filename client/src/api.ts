import { token } from '@/store/auth'

class ApiClient {
  private base = ''

  private headers(): HeadersInit {
    return {
      'Content-Type': 'application/json',
      ...(token.value ? { Authorization: `Bearer ${token.value}` } : {}),
    }
  }

  async get<T = any>(path: string, params?: Record<string, string>): Promise<T> {
    const url = params ? `${path}?${new URLSearchParams(params)}` : path
    const res = await fetch(url, { headers: this.headers() })
    if (res.status === 401) { import('@/store/auth').then(m => m.logout()); throw new Error('Unauthorized') }
    if (!res.ok) throw new Error(`API ${res.status}: ${path}`)
    return res.json()
  }

  async post<T = any>(path: string, body?: unknown): Promise<T> {
    const res = await fetch(path, {
      method: 'POST', headers: this.headers(),
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
    if (res.status === 401) { import('@/store/auth').then(m => m.logout()); throw new Error('Unauthorized') }
    if (!res.ok) throw new Error(`API ${res.status}: ${path}`)
    return res.json()
  }

  async put<T = any>(path: string, body?: unknown): Promise<T> {
    const res = await fetch(path, {
      method: 'PUT', headers: this.headers(),
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
    if (!res.ok) throw new Error(`API ${res.status}: ${path}`)
    return res.json()
  }

  async patch<T = any>(path: string, body?: unknown): Promise<T> {
    const res = await fetch(path, {
      method: 'PATCH', headers: this.headers(),
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
    if (!res.ok) throw new Error(`API ${res.status}: ${path}`)
    return res.json()
  }

  async delete(path: string): Promise<void> {
    await fetch(path, { method: 'DELETE', headers: this.headers() })
  }

  async upload<T = any>(path: string, body: FormData): Promise<T> {
    const headers: HeadersInit = token.value
      ? { Authorization: `Bearer ${token.value}` }
      : {}
    const res = await fetch(path, {
      method: 'POST',
      headers,
      body,
    })
    if (res.status === 401) { import('@/store/auth').then(m => m.logout()); throw new Error('Unauthorized') }
    if (!res.ok) throw new Error(`API ${res.status}: ${path}`)
    return res.json()
  }
}

export const api = new ApiClient()

/**
 * Append the bearer token as a ``?token=`` query parameter.
 *
 * The SPA authenticates via ``Authorization: Bearer …``, which the
 * ``fetch``-based ``api`` client carries automatically. Resources that
 * load via raw browser primitives — ``<img src>``, ``<video src>``,
 * ``<a href download>`` — can't carry a custom header, so they hit
 * authenticated endpoints unauthenticated and get a 401.
 *
 * The server's :class:`BearerTokenStrategy` accepts the token from
 * ``?token=`` as a fallback for exactly this reason (the same shape
 * the WebSocket uses). Operators must redact the query string from
 * access logs — see CLAUDE.md.
 *
 * No-op for absolute URLs (external resource, different origin).
 */
export function withAuthToken(url: string): string {
  if (!url || !token.value) return url
  if (url.startsWith('http://') || url.startsWith('https://') || url.startsWith('//')) {
    return url
  }
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}token=${encodeURIComponent(token.value)}`
}
