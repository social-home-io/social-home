import { signal, computed } from '@preact/signals'
import type { User } from '@/types'
import { api, _resetApiLoggedOut } from '@/api'

export const token       = signal<string | null>(localStorage.getItem('sh_token'))
export const currentUser = signal<User | null>(null)
// ``currentUser`` is only ever populated by a successful ``/api/me``,
// which itself requires authentication — so a non-null user is proof
// of an authenticated session. The token signal stays around for the
// bearer-mode flows (standalone, ha) and the WS query-string fallback,
// but no longer gates ``isAuthed``: under HA Supervisor ingress (haos
// mode) the SPA carries no token at all — ingress headers stand in
// for the bearer, and a successful ``/api/me`` is the only signal we
// have that the auth handshake worked.
export const isAuthed    = computed(() => currentUser.value !== null)

/**
 * Fetch the current user from `/api/me` and populate `currentUser`.
 *
 * Any code path that hands us a fresh token (login form, /setup
 * wizard, cold start with a stashed token) MUST follow up with this
 * call — otherwise ``isAuthed`` stays false and the SPA never
 * advances past the login screen.
 *
 * In haos mode the SPA carries no token; the request still goes out
 * (with no ``Authorization`` header) and HA Supervisor ingress adds
 * the headers the backend's ``HaIngressStrategy`` accepts. The
 * caller decides whether to attempt the probe based on the current
 * instance ``mode`` — :func:`App` is the only caller that does so.
 *
 * Returns the loaded User (or null on failure). Failures are silent
 * here; :mod:`api` already calls :func:`logout` on 401 *when a token
 * was attached*, so a bearer-mode session-expiry still toast-and-
 * redirects to login; an ingress-mode 401 stays quiet because it
 * means a deployment problem (the App shell renders a dedicated
 * error page for that), not a session timeout.
 */
export async function loadCurrentUser(): Promise<User | null> {
  try {
    const me = await api.get('/api/me') as User
    currentUser.value = me
    return me
  } catch {
    return null
  }
}

export function setToken(t: string) {
  token.value = t
  localStorage.setItem('sh_token', t)
  // A fresh sign-in means the next 401 should show its own toast.
  _resetApiLoggedOut()
}

export function logout() {
  token.value = null
  currentUser.value = null
  localStorage.removeItem('sh_token')
}
