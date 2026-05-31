/**
 * Presence store — household member presence driven by
 * `presence.updated` WS frames (§22).
 *
 * Carries two orthogonal signals:
 *   • Physical presence (``state`` / ``zone_name`` / GPS) from
 *     ``presence.updated``.
 *   • Session presence (``is_online`` / ``is_idle`` / ``last_seen_at``)
 *     from ``user.online`` / ``user.idle`` / ``user.offline``.
 *
 * The store is keyed by ``username`` for `presence.updated` lookups
 * but session-presence frames carry ``user_id`` only — we maintain a
 * secondary ``user_id → username`` index so both kinds of frame can
 * patch the same row.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'

export interface PresenceEntry {
  username:      string
  user_id?:      string
  display_name?: string
  picture_url?:  string | null
  state:         string
  zone_name?:    string | null
  latitude?:     number | null
  longitude?:    number | null
  is_online?:    boolean
  is_idle?:      boolean
  last_seen_at?: string | null
}

export const presence = signal<Record<string, PresenceEntry>>({})

function patchByUserId(
  user_id: string | undefined,
  patch: Partial<PresenceEntry>,
): void {
  if (!user_id) return
  const map = presence.value
  // Locate the entry by user_id. Bootstrapped /api/presence rows carry
  // both username + user_id, so this lookup hits in steady state.
  for (const username of Object.keys(map)) {
    if (map[username].user_id === user_id) {
      presence.value = { ...map, [username]: { ...map[username], ...patch } }
      return
    }
  }
  // No row yet for this user_id (session-presence frames carry only
  // ``user_id`` + ``last_seen_at`` — no username — and nothing may have
  // seeded the store before the first ``user.online``). UPSERT keyed by
  // ``user_id`` so the user appears (online/idle/offline) immediately;
  // a later ``presence.updated`` or ``loadPresence()`` merges the
  // username-keyed physical-presence row on top. We avoid clobbering a
  // future username-keyed row by keying the placeholder on the user_id
  // itself — ``loadPresence`` upsert-merges into the username slot and
  // drops the placeholder once a real row arrives.
  presence.value = {
    ...map,
    [user_id]: { username: user_id, user_id, state: 'unknown', ...patch },
  }
}

/** Seed (and refresh) the presence signal from ``GET /api/presence``.
 *
 *  Idempotent + upsert-merge: live WS state already in the signal is
 *  preserved (session flags from ``user.*`` frames win where the GET
 *  response is silent), so calling this after frames have arrived never
 *  clobbers them. Rows are keyed by ``username`` (matching
 *  ``presence.updated``); any placeholder row that an early
 *  ``user.*`` frame inserted under a bare ``user_id`` key is dropped
 *  once the canonical username-keyed row lands. */
export async function loadPresence(): Promise<void> {
  let rows: PresenceEntry[]
  try {
    rows = (await api.get('/api/presence')) as PresenceEntry[]
  } catch {
    // Auth not ready / presence feature disabled / transient — leave
    // whatever live WS state is already in the signal untouched.
    return
  }
  const map = { ...presence.value }
  // Drop user_id-keyed placeholders for users the GET now supplies a
  // real username-keyed row for, so we don't render the same person
  // twice (once as "u-anna", once as "anna").
  const incomingIds = new Set(rows.map(r => r.user_id).filter(Boolean))
  for (const key of Object.keys(map)) {
    if (key === map[key].user_id && incomingIds.has(key)) delete map[key]
  }
  for (const r of rows) {
    if (!r.username) continue
    // Existing row's live session flags win — the GET's is_online /
    // is_idle / last_seen_at may lag a just-arrived WS frame.
    map[r.username] = { ...r, ...map[r.username] }
  }
  presence.value = map
}

export function wirePresenceWs(): void {
  ws.on('presence.updated', (e) => {
    const data = e.data as unknown as PresenceEntry
    if (!data?.username) return
    presence.value = {
      ...presence.value,
      [data.username]: { ...presence.value[data.username], ...data },
    }
  })
  ws.on('user.online', (e) => {
    const data = e.data as { user_id?: string }
    patchByUserId(data.user_id, { is_online: true, is_idle: false })
  })
  ws.on('user.idle', (e) => {
    const data = e.data as { user_id?: string }
    patchByUserId(data.user_id, { is_online: true, is_idle: true })
  })
  ws.on('user.offline', (e) => {
    const data = e.data as { user_id?: string; last_seen_at?: string | null }
    patchByUserId(data.user_id, {
      is_online:    false,
      is_idle:      false,
      last_seen_at: data.last_seen_at ?? null,
    })
  })
}
