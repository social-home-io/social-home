/**
 * SpaceInviteDialog — generate and share an invite to a space.
 *
 * Three artifacts side-by-side: a copyable ``socialhome://invite#…``
 * code (primary — works cross-instance regardless of ingress shape),
 * an ingress-aware HTTPS link, and a QR (handy for same-room
 * handoffs).
 *
 * URL anchoring matters: the share link is built from
 * ``document.baseURI`` (honoured by `addBase`) so it works under HA
 * Supervisor ingress. Hard-coding ``location.origin`` would skip the
 * ingress prefix and bounce the iframe to HA Core's frontend.
 */
import { signal } from '@preact/signals'
import { useEffect } from 'preact/hooks'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
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

function buildLink(token: string): string {
  // ``addBase`` adds the ingress prefix; ``new URL`` resolves the
  // result against ``document.baseURI`` to produce the full URL.
  return new URL(addBase('/join?token=' + token), document.baseURI).toString()
}

function buildCode(token: string): string {
  return buildInviteCode({
    token,
    space_id: spaceId.value || null,
    space_display_hint: displayHint.value,
    // ``document.baseURI`` carries the ingress prefix; the joiner's
    // landing fallback uses this to detect "you may be on the wrong
    // instance" when the legacy ``/join?token=`` URL is followed.
    issuer_instance_url: document.baseURI,
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
  const link = token ? buildLink(token) : ''
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
              Pick whichever format the receiver finds easiest:
            </p>

            {/* Primary — the cross-instance code. Works regardless of
             *  the receiver's ingress / hosting shape because they
             *  paste it into their own SPA. */}
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

            {/* Secondary — clickable link with the ingress prefix. */}
            <div class="sh-invite-artifact">
              <div class="sh-invite-artifact-label">
                Link · clickable from email
              </div>
              <code class="sh-invite-link" data-testid="invite-link">{link}</code>
              <div class="sh-form-actions">
                <Button variant="secondary" onClick={() => copy(link, 'Link')}>
                  Copy link
                </Button>
              </div>
            </div>

            {/* Tertiary — QR for same-room handoff. */}
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
