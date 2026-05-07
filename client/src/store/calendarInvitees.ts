/**
 * Calendar invitees store (§23.60).
 *
 * Drives the cross-household "Invite" picker on
 * :class:`CalendarEventDialog`. Backed by ``GET /api/calendars/invitees``
 * which returns members of confirmed paired peer instances grouped
 * by instance.
 *
 * Local household members are intentionally NOT in this list — putting
 * an event on a household member's calendar is done via the dialog's
 * "For:" calendar selector (any household member can write to any
 * household member's calendar). The "Invite + RSVP" surface is reserved
 * for cross-household friends, where it actually carries new
 * information.
 *
 * Empty list (no instances paired yet) is a normal state — the picker
 * renders an empty-state CTA pointing at /settings/connections.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'

export interface InviteeMember {
  user_id: string
  instance_id: string
  remote_username: string
  display_name: string
  picture_hash: string | null
  picture_url: string | null
}

export interface InviteeInstance {
  instance_id: string
  instance_name: string
  members: InviteeMember[]
}

export const calendarInvitees = signal<InviteeInstance[]>([])
const loading = signal(false)
let loaded = false

export async function loadCalendarInvitees(force = false): Promise<void> {
  if (loaded && !force) return
  if (loading.value) return
  loading.value = true
  try {
    const data = await api.get('/api/calendars/invitees') as {
      instances: InviteeInstance[]
    }
    calendarInvitees.value = data?.instances ?? []
    loaded = true
  } catch {
    calendarInvitees.value = []
  } finally {
    loading.value = false
  }
}

/** Flat lookup by user_id — used to pretty-print a previously-saved
 *  attendee list when re-opening a draft event in the dialog. */
export function findInvitee(userId: string): InviteeMember | undefined {
  for (const inst of calendarInvitees.value) {
    for (const m of inst.members) {
      if (m.user_id === userId) return m
    }
  }
  return undefined
}
