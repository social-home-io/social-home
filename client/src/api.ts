import { token } from '@/store/auth'
import { showToast } from '@/components/Toast'

/**
 * Error thrown by ``ApiClient`` for non-2xx responses. Carries the HTTP
 * status code so consumers can branch on it (``e.status === 501`` to
 * detect a not-implemented endpoint, ``=== 404`` for missing data,
 * etc.) — previously the bare ``Error`` only carried the formatted
 * message string, forcing consumers to regex-match the message. The
 * ``HaUsersPanel`` standalone-mode banner relies on this; without the
 * status field its ``e?.status === 501`` check silently fell through
 * and the raw ``"API 501: /api/admin/ha-users"`` message landed in the
 * UI.
 */
export class ApiError extends Error {
  constructor(public readonly status: number, public readonly path: string) {
    super(`API ${status}: ${path}`)
    this.name = 'ApiError'
  }
}

class ApiClient {
  private base = ''

  private headers(): HeadersInit {
    return {
      'Content-Type': 'application/json',
      ...(token.value ? { Authorization: `Bearer ${token.value}` } : {}),
    }
  }

  /** Centralised handler for ``fetch`` responses. Surface a "Session
   *  expired" toast before logging the user out so the page doesn't
   *  silently wipe to the login form. Same path applies to every
   *  verb (GET/POST/PUT/PATCH/DELETE/upload) so an expired token is
   *  handled identically no matter which method tripped the 401. */
  private async _handle(res: Response, path: string): Promise<Response> {
    if (res.status === 401) {
      // Single banner per logout — repeated 401s in a render burst
      // shouldn't stack toasts on the way out.
      if (!ApiClient._loggingOut) {
        ApiClient._loggingOut = true
        showToast('Session expired — please sign in again', 'info')
        const auth = await import('@/store/auth')
        auth.logout()
      }
      throw new Error('Unauthorized')
    }
    if (!res.ok) throw new ApiError(res.status, path)
    return res
  }

  /** Latched flag so a burst of failed calls during logout doesn't
   *  fan out into a stack of "Session expired" toasts. Reset on the
   *  next successful auth (handled by the auth store on token set). */
  private static _loggingOut = false
  static resetLoggedOut(): void { ApiClient._loggingOut = false }

  async get<T = any>(path: string, params?: Record<string, string>): Promise<T> {
    const url = params ? `${path}?${new URLSearchParams(params)}` : path
    const res = await this._handle(
      await fetch(url, { headers: this.headers() }),
      path,
    )
    return res.json()
  }

  async post<T = any>(path: string, body?: unknown): Promise<T> {
    const res = await this._handle(
      await fetch(path, {
        method: 'POST', headers: this.headers(),
        body: body !== undefined ? JSON.stringify(body) : undefined,
      }),
      path,
    )
    return res.json()
  }

  async put<T = any>(path: string, body?: unknown): Promise<T> {
    const res = await this._handle(
      await fetch(path, {
        method: 'PUT', headers: this.headers(),
        body: body !== undefined ? JSON.stringify(body) : undefined,
      }),
      path,
    )
    return res.json()
  }

  async patch<T = any>(path: string, body?: unknown): Promise<T> {
    const res = await this._handle(
      await fetch(path, {
        method: 'PATCH', headers: this.headers(),
        body: body !== undefined ? JSON.stringify(body) : undefined,
      }),
      path,
    )
    return res.json()
  }

  async delete(path: string): Promise<void> {
    await this._handle(
      await fetch(path, { method: 'DELETE', headers: this.headers() }),
      path,
    )
  }

  async upload<T = any>(path: string, body: FormData): Promise<T> {
    const headers: HeadersInit = token.value
      ? { Authorization: `Bearer ${token.value}` }
      : {}
    const res = await this._handle(
      await fetch(path, {
        method: 'POST',
        headers,
        body,
      }),
      path,
    )
    return res.json()
  }
}

export const api = new ApiClient()
export const _resetApiLoggedOut = ApiClient.resetLoggedOut
