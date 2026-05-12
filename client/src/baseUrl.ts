/**
 * Ingress-aware URL helpers — anchor every runtime URL on the
 * document's base.
 *
 * Why this exists
 * ---------------
 *
 * The Social Home SPA can be served at one of three document bases
 * depending on the deployment shape:
 *
 *  - Standalone Docker / dev (Vite proxy): the document loads at
 *    ``http://host:port/`` and every API path is rooted at ``/api/...``.
 *  - HA add-on behind HA Supervisor ingress: the document loads at
 *    ``http://<ha>/api/hassio_ingress/<token>/`` and every API path
 *    on the add-on container ends up reachable through
 *    ``/api/hassio_ingress/<token>/api/...``.
 *
 * Hard-coding ``/api/me`` or ``new WebSocket(\`ws://${location.host}/api/ws\`)``
 * worked for the first shape and silently broke under ingress —
 * fetches would go to ``/api/me`` on the HA host (which is HA Core,
 * not the add-on) and 404 / 401 / redirect.
 *
 * Fix: backend's ``SpaIndexView`` rewrites ``<base href>`` in
 * ``index.html`` from the Supervisor-injected ``X-Ingress-Path``
 * header at request time; the SPA reads ``document.baseURI`` (which
 * honours ``<base href>``) and builds every fetch / WebSocket / link
 * relative to it.
 *
 * ``document.baseURI`` is evaluated **once** at module load so it
 * survives a programmatic ``history.pushState`` (the document's base
 * doesn't change mid-session).
 */

/** Trailing-slash-terminated absolute document base, e.g.
 *  ``"http://homeassistant.local:8123/api/hassio_ingress/abc/"`` or
 *  ``"http://localhost:7123/"``. Always ends in ``/``. */
const DOCUMENT_BASE = new URL('./', document.baseURI).toString()

/** Path portion of the document base with leading ``/`` and trailing
 *  ``/``, e.g. ``"/api/hassio_ingress/abc/"`` or ``"/"``. Used by
 *  the router to strip the prefix before matching. */
export const basePath: string = new URL(DOCUMENT_BASE).pathname

/** Build an absolute HTTP URL anchored on ``document.baseURI``.
 *
 *  Accepts both leading-``/`` paths (legacy callers) and bare
 *  segments. ``new URL`` would otherwise treat a leading-``/`` path
 *  as origin-rooted (ignoring the base), so strip it first. */
export function apiUrl(path: string): string {
  return new URL(path.replace(/^\/+/, ''), DOCUMENT_BASE).toString()
}

/** Build a ``ws://`` or ``wss://`` URL anchored on the document base.
 *
 *  ``new URL`` doesn't accept a ``ws://`` base, so we resolve against
 *  the HTTP base first, then rewrite the protocol. */
export function wsUrl(path: string): string {
  const u = new URL(path.replace(/^\/+/, ''), DOCUMENT_BASE)
  u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:'
  return u.toString()
}

/** Strip ``basePath`` from a pathname for client-side router matching.
 *
 *  ``preact-iso``'s ``<Router>`` matches against
 *  ``location.pathname`` directly. Under ingress that's the full
 *  ``/api/hassio_ingress/<token>/feed`` — we need to feed
 *  ``/feed`` into the matcher. */
export function stripBase(pathname: string): string {
  // ``basePath`` always ends in ``/``. To preserve the leading slash
  // in the result we keep the trailing slash on the prefix when
  // stripping ("foo/" off "/foo/feed" gives "feed" — we want "/feed",
  // so strip without the trailing slash and let the leading "/" of
  // the suffix carry through).
  const prefix = basePath.replace(/\/+$/, '')
  if (!prefix) return pathname
  if (pathname === prefix) return '/'
  if (pathname.startsWith(prefix + '/')) {
    return pathname.slice(prefix.length) || '/'
  }
  return pathname
}

/** Add ``basePath`` to a path for ``history.pushState`` / link clicks.
 *
 *  Idempotent — passing a path that already starts with ``basePath``
 *  returns it unchanged. */
export function addBase(path: string): string {
  // Strip query / hash first so we don't break composition.
  const [pathOnly, ...rest] = path.split(/(?=[?#])/, 2)
  const suffix = rest.join('')
  const prefix = basePath.replace(/\/+$/, '')
  if (!prefix) return path
  if (pathOnly.startsWith(basePath) || pathOnly === prefix) return path
  const normalised = pathOnly.startsWith('/') ? pathOnly : '/' + pathOnly
  return prefix + normalised + suffix
}
