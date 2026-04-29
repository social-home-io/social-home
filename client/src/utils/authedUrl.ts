/**
 * Append the bearer token as a ``?token=`` query parameter.
 *
 * The SPA authenticates via ``Authorization: Bearer …``, which the
 * ``fetch``-based ``api`` client carries automatically. Resources
 * that load via raw browser primitives — ``<img src>``,
 * ``<video src>``, ``<a href download>`` — can't carry a custom
 * header, so they hit authenticated endpoints unauthenticated and
 * get a 401.
 *
 * The server's :class:`BearerTokenStrategy` accepts the token from
 * ``?token=`` as a fallback for exactly this reason (the same shape
 * the WebSocket uses). Operators must redact the query string from
 * access logs — the central ``RedactingAccessLogger`` already does
 * this for ``token``, ``api_key``, ``access_token`` and
 * ``password``, so adding the param to media URLs doesn't leak
 * secrets.
 *
 * Lives in ``utils/`` rather than ``api.ts`` so test files that
 * ``vi.mock('@/api', ...)`` don't have to add the helper to every
 * mock — the import path is intentionally separate.
 *
 * No-op for absolute URLs (external resource, different origin) and
 * pre-login (token unset).
 */
import { token } from '@/store/auth'

export function withAuthToken(url: string): string {
  if (!url || !token.value) return url
  if (
    url.startsWith('http://') ||
    url.startsWith('https://') ||
    url.startsWith('//')
  ) {
    return url
  }
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}token=${encodeURIComponent(token.value)}`
}
