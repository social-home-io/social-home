/**
 * SpaceInviteDialog — generate and share an invite to a space.
 *
 * Two artifacts: a copyable ``socialhome://invite#…`` code (primary
 * — paste into chat) and a QR (handy for same-room handoff). The
 * code embeds the issuer's stable ``instance_id`` so the receiver's
 * own Social Home can route the redeem over federation when the
 * issuer isn't on the receiver's instance.
 *
 * Why no clickable HTTPS link? The receiver has to be on **their
 * own** instance to redeem — a URL to the issuer's instance can't
 * call the redeem RPC on the receiver's behalf. A future GFS-
 * mediated redirect could lift that; until then offering a link
 * that doesn't work is worse than not offering one at all.
 */
import { signal } from '@preact/signals'
import { useEffect } from 'preact/hooks'
import { api } from '@/api'
import { instanceConfig } from '@/store/instance'
import { buildInviteCode } from '@/lib/spaceInviteCode'
import { Modal } from './Modal'
import { Button } from './Button'
import { QrCodeImg } from './QrCodeImg'
import { showToast } from './Toast'

const open = signal(false)
const spaceId = signal('')
const displayHint = signal<string | null>(null)
const inviteToken = signal('')
const uses = signal(1)
const loading = signal(false)

/**
 * Open the invite dialog for ``sid``. ``hint`` is the space's display
 * name — pass it when the caller already has it (avoids an extra
 * fetch). If omitted, the dialog fetches ``/api/spaces/{id}`` on mount
 * so the embedded code carries a meaningful preview hint.
 */
export function openSpaceInvite(sid: string, hint: string | null = null) {
  spaceId.value = sid
  displayHint.value = hint
  inviteToken.value = ''
  uses.value = 1
  open.value = true
}

function buildCode(token: string): string {
  return buildInviteCode({
    token,
    space_id: spaceId.value || null,
    space_display_hint: displayHint.value,
    // Stable instance id so the receiver can decide whether they can
    // redeem locally (same instance), over federation (CONFIRMED peer),
    // or need to pair first. ``null`` only on pre-setup cold start,
    // which shouldn't happen here since you have to be authed to
    // open this dialog.
    issuer_instance_id: instanceConfig.value?.instance_id ?? null,
  })
}

export function SpaceInviteDialog() {
  // Fetch the space name once the dialog opens if the caller didn't
  // pass a hint — so the embedded ``space_display_hint`` is meaningful.
  useEffect(() => {
    if (!open.value || displayHint.value || !spaceId.value) return
    let cancelled = false
    api.get(`/api/spaces/${spaceId.value}`).then((data) => {
      if (cancelled) return
      const name = (data as { name?: string }).name
      if (name) displayHint.value = name
    }).catch(() => { /* swallow — hint is optional */ })
    return () => { cancelled = true }
  }, [open.value, spaceId.value])

  const token = inviteToken.value
  const code = token ? buildCode(token) : ''

  const createToken = async () => {
    loading.value = true
    try {
      const result = await api.post(
        `/api/spaces/${spaceId.value}/invite-tokens`,
        { uses: uses.value },
      ) as { token: string }
      inviteToken.value = result.token
    } catch (e: unknown) {
      showToast((e as Error)?.message ?? 'Failed to create invite', 'error')
    } finally {
      loading.value = false
    }
  }

  const copy = async (text: string, label: string) => {
    try {
      await navigator.clipboard.writeText(text)
      showToast(`${label} copied!`, 'success')
    } catch {
      showToast(`Could not copy — select the ${label.toLowerCase()} to copy manually.`, 'error')
    }
  }

  const reset = () => {
    inviteToken.value = ''
    uses.value = 1
  }

  return (
    <Modal open={open.value} onClose={() => open.value = false}
           title="Invite to space">
      <div class="sh-invite-dialog">
        {!token ? (
          <>
            <p class="sh-muted" style={{ marginTop: 0 }}>
              Generate an invite. The receiver pastes the code on their
              own Social Home → Spaces page, or follows the link if you
              share it via email.
            </p>
            <label>
              How many people can use it?
              <input
                type="number"
                min={1}
                max={100}
                value={uses.value}
                onInput={(e) =>
                  uses.value = parseInt(
                    (e.target as HTMLInputElement).value, 10,
                  ) || 1
                }
              />
            </label>
            <div class="sh-form-actions">
              <Button onClick={createToken} loading={loading.value}>
                Generate invite
              </Button>
            </div>
          </>
        ) : (
          <>
            <p class="sh-muted" style={{ marginTop: 0 }}>
              Good for {uses.value} {uses.value === 1 ? 'use' : 'uses'}.
              The receiver pastes this into their own Social Home →
              Spaces → "Join with invite code".
            </p>

            <div class="sh-invite-artifact sh-invite-artifact--primary">
              <div class="sh-invite-artifact-label">
                Invite code · paste into chat
              </div>
              <code class="sh-invite-link" data-testid="invite-code">{code}</code>
              <div class="sh-form-actions">
                <Button onClick={() => copy(code, 'Code')}>
                  Copy code
                </Button>
              </div>
            </div>

            <div class="sh-invite-artifact sh-invite-artifact--qr">
              <div class="sh-invite-artifact-label">
                QR · scan with another device
              </div>
              <QrCodeImg data={code} size={180} alt="Invite QR code" />
            </div>

            <div class="sh-form-actions">
              <Button variant="secondary" onClick={reset}>
                Make another
              </Button>
            </div>
          </>
        )}
      </div>
    </Modal>
  )
}
