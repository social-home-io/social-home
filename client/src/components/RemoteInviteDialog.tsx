/**
 * RemoteInviteDialog — "Invite someone from another household" modal
 * for private-space admins (§D1b).
 *
 * Replaces the old "pick a household → type a user_id" form with a
 * single typeahead over confirmed peers' member directories sourced
 * from ``/api/friends``. Peer-user-sync surfaces remote users on the
 * inviter's side, so the admin can now pick a real person by name
 * instead of guessing an opaque user id.
 *
 * On submit, the dialog derives ``invitee_instance_id`` +
 * ``invitee_user_id`` from the selected row and POSTs the existing
 * ``/api/spaces/{id}/remote-invites`` endpoint — backend unchanged.
 */
import { useEffect, useMemo, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Modal } from './Modal'
import { Button } from './Button'
import { showToast } from './Toast'

interface FriendsHouseholdMember {
  user_id: string
  instance_id?: string  // present on remote rows
  remote_username?: string
  display_name: string
  last_seen_at?: string | null
}

interface FriendsHousehold {
  instance_id: string
  display_name: string
  status: string
  reachable: boolean
  members: FriendsHouseholdMember[]
}

interface FriendsResponse {
  instance: {
    instance_id: string
    display_name: string
    members: FriendsHouseholdMember[]
  }
  households: FriendsHousehold[]
}

interface PickRow {
  user_id: string
  instance_id: string
  display_name: string
  household_name: string
  last_seen_at: string | null
}

const open = signal<string | null>(null) // holds the space_id being invited to

export function openRemoteInviteDialog(spaceId: string) {
  open.value = spaceId
}

function relativeSince(iso: string | null): string {
  if (!iso) return 'never seen'
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return 'never seen'
  const diff = Date.now() - t
  const min = Math.floor(diff / 60_000)
  if (min < 1) return 'just now'
  if (min < 60) return `${min} min ago`
  const hr = Math.floor(min / 60)
  if (hr < 24) return `${hr} h ago`
  const day = Math.floor(hr / 24)
  if (day < 7) return `${day} d ago`
  return `${Math.floor(day / 7)} wk ago`
}

export function RemoteInviteDialog() {
  const [rows, setRows] = useState<PickRow[]>([])
  const [query, setQuery] = useState('')
  const [pickedId, setPickedId] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!open.value) return
    setLoading(true)
    setError(null)
    api.get('/api/friends').then((raw) => {
      const data = raw as FriendsResponse
      const flat: PickRow[] = (data.households || [])
        .filter((h) => h.status === 'confirmed' || h.status === 'active')
        .flatMap((h) => h.members.map((m) => ({
          user_id: m.user_id,
          // For confirmed remote members ``instance_id`` is on the row;
          // fall back to the household's id (always present).
          instance_id: m.instance_id ?? h.instance_id,
          display_name: m.display_name,
          household_name: h.display_name,
          last_seen_at: m.last_seen_at ?? null,
        })))
      setRows(flat)
    }).catch(() => {
      setRows([])
    }).finally(() => setLoading(false))
  }, [open.value])

  // Stable composite-key the picker uses to identify a row, since
  // ``user_id`` alone is not unique across households.
  const rowKey = (r: PickRow) => `${r.instance_id}:${r.user_id}`

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return rows
    return rows.filter((r) => (
      r.display_name.toLowerCase().includes(q)
      || r.household_name.toLowerCase().includes(q)
    ))
  }, [rows, query])

  if (!open.value) return null

  const spaceId = open.value
  const close = () => {
    open.value = null
    setQuery(''); setPickedId(null); setError(null)
  }

  const submit = async () => {
    const picked = rows.find((r) => rowKey(r) === pickedId)
    if (!picked) {
      setError('Pick someone from the list first.')
      return
    }
    setSubmitting(true); setError(null)
    try {
      await api.post(`/api/spaces/${spaceId}/remote-invites`, {
        invitee_instance_id: picked.instance_id,
        invitee_user_id: picked.user_id,
      })
      showToast(`Invite sent to ${picked.display_name}`, 'success')
      close()
    } catch (exc) {
      setError((exc as Error).message || 'Failed to send')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal open={true} onClose={close} title="Invite from another household">
      {loading ? (
        <p class="sh-muted">Loading paired households…</p>
      ) : rows.length === 0 ? (
        <p class="sh-muted">
          You need at least one paired household with visible members
          before you can send a cross-household invite.
        </p>
      ) : (
        <>
          <label class="sh-form-field">
            <span>Find someone</span>
            <input
              type="search"
              value={query}
              placeholder="Type a name or household…"
              onInput={(e) => setQuery((e.target as HTMLInputElement).value)}
              autoFocus
              data-testid="remote-invite-search"
            />
          </label>
          <div class="sh-remote-invite-picker" role="listbox"
               aria-label="Paired-household members">
            {matches.length === 0 ? (
              <p class="sh-muted sh-remote-invite-picker__empty">
                No matches. Try a different name.
              </p>
            ) : (
              matches.map((r) => {
                const id = rowKey(r)
                const picked = pickedId === id
                return (
                  <button
                    key={id}
                    type="button"
                    role="option"
                    aria-selected={picked}
                    class={[
                      'sh-remote-invite-row',
                      picked ? 'sh-remote-invite-row--picked' : '',
                    ].filter(Boolean).join(' ')}
                    onClick={() => setPickedId(id)}
                    data-testid={`remote-invite-row-${r.user_id}`}
                  >
                    <span class="sh-remote-invite-row__name">
                      {r.display_name}
                    </span>
                    <span class="sh-remote-invite-row__meta">
                      {r.household_name} · {r.last_seen_at
                        ? `last seen ${relativeSince(r.last_seen_at)}`
                        : 'never seen'}
                    </span>
                  </button>
                )
              })
            )}
          </div>
          {error && <p class="sh-error">{error}</p>}
          <div class="sh-modal-actions">
            <Button variant="secondary" onClick={close} disabled={submitting}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={submit}
              loading={submitting}
              disabled={!pickedId}
            >
              Send invite
            </Button>
          </div>
        </>
      )}
    </Modal>
  )
}
