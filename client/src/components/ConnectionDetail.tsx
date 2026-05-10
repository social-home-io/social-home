/**
 * ConnectionDetail — per-connection settings (§23.88, §23.89, §23.90).
 */
import { signal } from '@preact/signals'
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { Modal } from './Modal'
import { Button } from './Button'
import { ConfirmDialog } from './ConfirmDialog'
import { Spinner } from './Spinner'
import { showToast } from './Toast'

interface Connection {
  instance_id: string; display_name: string; status: string
  inbox_url: string; intro_relay_enabled: boolean
  unreachable_since: string | null; paired_at: string | null
}

interface VisibleUser {
  user_id: string
  username: string
  display_name: string
  is_admin: boolean
  visible: boolean
}

const showRevoke = signal(false)

export function ConnectionDetail({ conn, onClose, onRevoke }: {
  conn: Connection; onClose: () => void; onRevoke: () => void
}) {
  const [visUsers, setVisUsers] = useState<VisibleUser[] | null>(null)
  const [visBusy, setVisBusy] = useState<Set<string>>(new Set())

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const body = await api.get(
          `/api/pairing/connections/${conn.instance_id}/visible-users`,
        ) as { users: VisibleUser[] }
        if (!cancelled) setVisUsers(body.users)
      } catch {
        if (!cancelled) {
          // 403 (non-admin) or 404 (peer no longer confirmed) — silently
          // hide the section rather than scary-error the whole modal.
          setVisUsers([])
        }
      }
    })()
    return () => { cancelled = true }
  }, [conn.instance_id])

  const toggleVisibility = async (u: VisibleUser) => {
    const next = !u.visible
    setVisBusy(b => new Set(b).add(u.user_id))
    try {
      const body = await api.patch(
        `/api/pairing/connections/${conn.instance_id}/visible-users`,
        { updates: [{ user_id: u.user_id, visible: next }] },
      ) as { users: VisibleUser[] }
      setVisUsers(body.users)
      showToast(
        next
          ? `${u.display_name} is now visible to ${conn.display_name}`
          : `${u.display_name} is now hidden from ${conn.display_name}`,
        'success',
      )
    } catch (e: any) {
      showToast(e.message || 'Failed to update visibility', 'error')
    } finally {
      setVisBusy(b => {
        const n = new Set(b)
        n.delete(u.user_id)
        return n
      })
    }
  }

  const toggleRelay = async () => {
    try {
      await api.patch(`/api/pairing/connections/${conn.instance_id}/settings`, {
        intro_relay_enabled: !conn.intro_relay_enabled,
      })
      showToast('Setting updated', 'success')
    } catch (e: any) { showToast(e.message || 'Failed', 'error') }
  }

  const revoke = async () => {
    try {
      await api.delete(`/api/pairing/connections/${conn.instance_id}`)
      showToast('Connection revoked', 'info')
      showRevoke.value = false
      onRevoke()
    } catch (e: any) { showToast(e.message || 'Failed', 'error') }
  }

  return (
    <Modal open={true} onClose={onClose} title={conn.display_name}>
      <div class="sh-connection-detail">
        <dl>
          <dt>Instance ID</dt><dd class="sh-mono">{conn.instance_id}</dd>
          <dt>Status</dt><dd class={`sh-status sh-status--${conn.status}`}>{conn.status}</dd>
          <dt>Inbox</dt><dd class="sh-mono sh-muted">{conn.inbox_url}</dd>
          {conn.paired_at && <><dt>Paired</dt><dd>{new Date(conn.paired_at).toLocaleString()}</dd></>}
          {conn.unreachable_since && (
            <><dt>Unreachable since</dt><dd class="sh-text-warning">{new Date(conn.unreachable_since).toLocaleString()}</dd></>
          )}
        </dl>
        <label class="sh-toggle-row">
          <input type="checkbox" checked={conn.intro_relay_enabled} onChange={toggleRelay} />
          Allow introduced pairing (friend-of-a-friend)
        </label>

        {visUsers !== null && visUsers.length > 0 && (
          <>
            <hr />
            <div class="sh-visible-users">
              <h4 style={{ margin: '0 0 4px' }}>
                Who's visible to {conn.display_name}?
              </h4>
              <p class="sh-muted" style={{ marginTop: 0, fontSize: 'var(--sh-font-size-sm)' }}>
                Unticked members stop showing up here. Existing posts in
                shared spaces stay; future profile updates, presence,
                and DMs from hidden members are filtered.
              </p>
              <ul class="sh-visible-users-list" style={{ listStyle: 'none', padding: 0, margin: 0 }}>
                {visUsers.map(u => (
                  <li key={u.user_id} class="sh-visible-users-row">
                    <label class="sh-toggle-row">
                      <input
                        type="checkbox"
                        checked={u.visible}
                        disabled={visBusy.has(u.user_id)}
                        onChange={() => void toggleVisibility(u)}
                      />
                      <span>
                        {u.display_name || u.username}
                        {u.is_admin && (
                          <span class="sh-muted" style={{ marginLeft: 'var(--sh-space-xs)', fontSize: 'var(--sh-font-size-xs)' }}>
                            admin
                          </span>
                        )}
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
            </div>
          </>
        )}
        {visUsers === null && (
          <div style={{ textAlign: 'center', padding: 'var(--sh-space-sm)' }}>
            <Spinner />
          </div>
        )}

        <hr />
        <Button variant="danger" onClick={() => showRevoke.value = true}>Revoke connection</Button>
      </div>
      <ConfirmDialog open={showRevoke.value} title="Revoke connection?"
        message="This will permanently disconnect this household. All shared spaces will stop syncing. You'll need to re-pair via QR to reconnect."
        confirmLabel="Revoke" destructive onConfirm={revoke}
        onCancel={() => showRevoke.value = false} />
    </Modal>
  )
}
