/**
 * RemoteInviteInboxBanner — surfaces pending inbound private-space
 * invitations from other households (§D1b). Sits at the top of the
 * space list so a user who just received one doesn't miss it.
 *
 * One-click Accept consumes the invite token and seats the user as a
 * remote member of the host's private space. Decline tells the host
 * via ``SPACE_PRIVATE_INVITE_DECLINE``.
 *
 * UX notes:
 *
 * * The inviter's household is shown by its human-readable display
 *   name (looked up against ``/api/friends``) — the raw
 *   ``inviter_instance_id`` hex would be unreadable and the user
 *   couldn't tell *who* is inviting them. When the lookup misses
 *   (peer-directory hasn't synced yet, or the inviter isn't on a
 *   confirmed household), we fall back to a short hash so the
 *   distinguishing prefix is still visible.
 * * Accept + Decline are disabled while either is in flight on the
 *   same invite. Mesh-routed accepts can take 5-10 s end-to-end
 *   (discovery probe + SPACE_ROUTED forward leg + ACK leg) — without
 *   the disable, an impatient user can double-click Accept or click
 *   Decline mid-Accept, racing the backend.
 * * Decline is a two-step confirm (click → "Confirm decline" → click
 *   again). A single misclick used to fire SPACE_PRIVATE_INVITE_DECLINE
 *   and remove the invitation row with no undo; the second-click
 *   pattern is the lightest-weight way to add a guard without
 *   shipping a full confirm dialog. The confirm state auto-clears
 *   after 4 s so a paused user doesn't accidentally confirm later.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { Button } from './Button'
import { showToast } from './Toast'
import type { RemoteInvite } from '@/types'

interface HouseholdRow {
  instance_id: string
  display_name?: string
  local_alias?: string | null
  federated_display_name?: string | null
}

interface FriendsResponse {
  households?: HouseholdRow[]
}

type InFlightState = Record<string, 'accept' | 'decline' | null>
type ConfirmState = Record<string, boolean>

const CONFIRM_RESET_MS = 4000

export function RemoteInviteInboxBanner() {
  const [invites, setInvites] = useState<RemoteInvite[]>([])
  const [households, setHouseholds] = useState<Map<string, HouseholdRow>>(
    () => new Map(),
  )
  const [inFlight, setInFlight] = useState<InFlightState>({})
  const [confirming, setConfirming] = useState<ConfirmState>({})

  const load = async () => {
    // Run the two lookups in parallel — they share no data and the
    // banner needs both before it can render a meaningful row.
    const [invitesRes, friendsRes] = await Promise.allSettled([
      api.get('/api/remote_invites'),
      api.get('/api/friends'),
    ])

    if (invitesRes.status === 'fulfilled') {
      setInvites(invitesRes.value as RemoteInvite[])
    } else {
      setInvites([])
    }
    if (friendsRes.status === 'fulfilled') {
      const hs = (friendsRes.value as FriendsResponse).households || []
      const map = new Map<string, HouseholdRow>()
      for (const h of hs) {
        if (h.instance_id) map.set(h.instance_id, h)
      }
      setHouseholds(map)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  if (invites.length === 0) return null

  const householdLabel = (instance_id: string): string => {
    const row = households.get(instance_id)
    if (row) {
      // local alias wins over federated name (admin renamed the
      // household locally) — same precedence the Friends page uses.
      const name = row.local_alias || row.display_name || row.federated_display_name
      if (name) return name
    }
    // Lookup miss — peer-directory hasn't synced yet, or the inviter
    // isn't on any confirmed household. Show a short hash so two
    // distinct instances still look distinct.
    return `${instance_id.slice(0, 8)}…`
  }

  const decide = async (
    invite: RemoteInvite,
    decision: 'accept' | 'decline',
  ) => {
    setInFlight((prev) => ({ ...prev, [invite.invite_token]: decision }))
    try {
      await api.post(
        `/api/remote_invites/${invite.invite_token}/${decision}`, {},
      )
      showToast(
        decision === 'accept' ? 'Invite accepted' : 'Invite declined',
        decision === 'accept' ? 'success' : 'info',
      )
      await load()
    } catch (exc) {
      showToast((exc as Error).message, 'error')
    } finally {
      setInFlight((prev) => ({ ...prev, [invite.invite_token]: null }))
      // Drop the confirm-pending flag too in case decline failed —
      // user can retry from a clean slate.
      setConfirming((prev) => ({ ...prev, [invite.invite_token]: false }))
    }
  }

  const handleDecline = (invite: RemoteInvite) => {
    if (confirming[invite.invite_token]) {
      void decide(invite, 'decline')
      return
    }
    setConfirming((prev) => ({ ...prev, [invite.invite_token]: true }))
    // Reset the confirm flag after a short window so a user who got
    // distracted doesn't accidentally fire decline on their next
    // click two minutes later.
    setTimeout(() => {
      setConfirming((prev) => ({ ...prev, [invite.invite_token]: false }))
    }, CONFIRM_RESET_MS)
  }

  return (
    <aside class="sh-remote-invite-banner">
      <h2>📬 Pending invites from other households</h2>
      {invites.map((inv) => {
        const busy = inFlight[inv.invite_token] != null
        const declineConfirming = confirming[inv.invite_token] === true
        return (
          <div key={inv.invite_token} class="sh-remote-invite-banner__row">
            <div>
              <strong>{inv.space_display_hint || inv.space_id}</strong>
              <span class="sh-muted">
                {' '}from <strong>{householdLabel(inv.inviter_instance_id)}</strong>
              </span>
            </div>
            <div class="sh-remote-invite-banner__actions">
              <Button
                variant="secondary"
                onClick={() => handleDecline(inv)}
                disabled={busy}
                loading={inFlight[inv.invite_token] === 'decline'}
                data-testid="invite-decline"
              >
                {declineConfirming ? 'Confirm decline' : 'Decline'}
              </Button>
              <Button
                variant="primary"
                onClick={() => void decide(inv, 'accept')}
                disabled={busy}
                loading={inFlight[inv.invite_token] === 'accept'}
                data-testid="invite-accept"
              >
                Accept
              </Button>
            </div>
          </div>
        )
      })}
    </aside>
  )
}
