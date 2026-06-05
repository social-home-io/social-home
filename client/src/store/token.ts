import { signal } from '@preact/signals'

/**
 * Bearer token (standalone / ha modes; absent under haos ingress).
 *
 * Lives in its own dependency-free module so both :mod:`api` and
 * :mod:`store/auth` can import it without forming an ``api.ts ↔ auth.ts``
 * import cycle (``api`` needs the token for the auth header; ``auth`` needs
 * ``api`` to call ``/api/me``). ``store/auth`` re-exports it so existing
 * ``import { token } from '@/store/auth'`` call sites keep working.
 */
export const token = signal<string | null>(localStorage.getItem('sh_token'))
