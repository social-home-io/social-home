/**
 * ``socialhome://invite#<base64url(JSON)>`` — single-line, chat-safe
 * space-invite handoff.
 *
 * Mirrors the §11 pairing scheme (``socialhome://pair#…``). The JSON
 * payload sits in the URL fragment so a stray paste into a browser
 * address bar never sends the token to anyone's server logs —
 * fragments stay client-side.
 *
 * Payload fields:
 *
 *  - ``token`` — the bearer credential the receiver POSTs to
 *    ``/api/spaces/join``. Mandatory.
 *  - ``space_id`` — for client-side preview / post-join navigation.
 *    Optional for back-compat with bare-token pastes.
 *  - ``space_display_hint`` — human-readable space name so the join
 *    card can render "You're about to join Pascal's family" without
 *    a round-trip to the issuer. Optional.
 *  - ``issuer_instance_url`` — the document.baseURI of the issuing
 *    instance. Lets the receiver detect "you landed on the wrong
 *    instance" when the same code is opened via the legacy
 *    ``/join?token=…`` URL. Optional.
 *
 * Backend wire contract is unchanged — only ``token`` ever travels in
 * the HTTP body. All metadata sits on top of the token for SPA-side
 * UX.
 */
import { base64UrlEncode, base64UrlDecode } from './base64Url'

export interface SpaceInvitePayload {
  token: string
  space_id?: string | null
  space_display_hint?: string | null
  issuer_instance_url?: string | null
}

export function buildInviteCode(payload: SpaceInvitePayload): string {
  return `socialhome://invite#${base64UrlEncode(JSON.stringify(payload))}`
}

const URI_PREFIX = 'socialhome://invite#'

// Matches a bare invite token. The backend mints tokens via
// ``secrets.token_hex(16)`` (or similar), so this is conservative —
// 16+ hex chars without any other punctuation.
const BARE_TOKEN_RE = /^[a-f0-9]{16,64}$/i

function isPayloadValid(raw: unknown): raw is SpaceInvitePayload {
  if (!raw || typeof raw !== 'object') return false
  const obj = raw as Record<string, unknown>
  if (typeof obj.token !== 'string' || !obj.token) return false
  return true
}

/**
 * Decode a pasted invite string. Accepts, in order of preference:
 *
 *   1. ``socialhome://invite#<base64url(JSON)>`` — the canonical wire
 *      shape this module emits.
 *   2. Raw multi-field JSON — forward-compat with codes shared via
 *      other transports (chat that mangles the URI scheme).
 *   3. A bare hex token — back-compat with operators who pasted just
 *      the UUID from an older share dialog. We synthesise a minimal
 *      ``{token}`` payload so the join card can still call the API.
 *
 * Returns ``null`` for anything else (garbage, empty, wrong scheme).
 */
export function decodeInviteCode(raw: string): SpaceInvitePayload | null {
  const trimmed = raw.trim()
  if (!trimmed) return null
  if (trimmed.startsWith(URI_PREFIX)) {
    const json = base64UrlDecode(trimmed.slice(URI_PREFIX.length))
    if (!json) return null
    try {
      const parsed = JSON.parse(json) as unknown
      return isPayloadValid(parsed) ? parsed : null
    } catch {
      return null
    }
  }
  if (trimmed.startsWith('{')) {
    try {
      const parsed = JSON.parse(trimmed) as unknown
      return isPayloadValid(parsed) ? parsed : null
    } catch {
      return null
    }
  }
  if (BARE_TOKEN_RE.test(trimmed)) {
    return { token: trimmed }
  }
  return null
}
