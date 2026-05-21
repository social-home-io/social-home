/**
 * SpaceJoinByCodeDialog — modal for pasting an invite code (or
 * scanning a QR) to join a space.
 *
 * Opens via :func:`openSpaceJoinByCode` from the Spaces dashboard
 * header (next to "+ Create space"). Mirrors the §11 household
 * pairing dialog's UX: a two-tile method picker (📷 Scan QR / 📋
 * Paste code) at the top, the active panel below. Same class names
 * (``sh-pairing-method-grid``, ``sh-pairing-method-card``) so the
 * visual stays in lockstep with pairing's muscle memory.
 *
 * Decoder accepts the new ``socialhome://invite#…`` shape, raw JSON,
 * and bare hex tokens (see :mod:`spaceInviteCode`). Three distinct
 * failure modes get three distinct messages.
 */
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api, ApiError } from '@/api'
import { addBase } from '@/baseUrl'
import { decodeInviteCode } from '@/lib/spaceInviteCode'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { QrScanner } from '@/components/QrScanner'
import { t } from '@/i18n/i18n'

type InviteMethod = 'paste' | 'qr'

const open = signal(false)
const method = signal<InviteMethod>('paste')
const draft = signal('')
const submitting = signal(false)
const errorMsg = signal<string | null>(null)

export function openSpaceJoinByCode() {
  // Reset state so a re-open after a prior cancel / error starts clean.
  method.value = 'paste'
  draft.value = ''
  submitting.value = false
  errorMsg.value = null
  open.value = true
}

function isWrongInstance(payload: ReturnType<typeof decodeInviteCode>): boolean {
  if (!payload?.issuer_instance_url) return false
  // The invite was minted by ``document.baseURI`` of the issuer. If
  // that doesn't match our own ``document.baseURI``, the receiver is
  // either on a different household or a different ingress tunnel —
  // either way the join will fail and we should pre-flight that with
  // a more honest error than ``404`` from the API.
  try {
    const ours = new URL('.', document.baseURI).href
    const theirs = new URL('.', payload.issuer_instance_url).href
    return ours !== theirs
  } catch {
    return false
  }
}

export function SpaceJoinByCodeDialog() {
  const loc = useLocation()

  const close = () => {
    open.value = false
  }

  const pickMethod = (m: InviteMethod) => {
    errorMsg.value = null
    method.value = m
  }

  const submit = async (raw?: string) => {
    const input = (raw ?? draft.value).trim()
    if (!input) {
      errorMsg.value = 'Paste a code or scan a QR first.'
      return
    }
    errorMsg.value = null
    const payload = decodeInviteCode(input)
    if (!payload) {
      errorMsg.value = "That doesn't look like a Social Home invite code."
      return
    }
    if (isWrongInstance(payload)) {
      errorMsg.value = (
        'This invite is for another Social Home instance — open your ' +
        'own home and paste it from there.'
      )
      return
    }
    submitting.value = true
    try {
      const r = await api.post('/api/spaces/join', { token: payload.token }) as {
        space_id: string
      }
      open.value = false
      draft.value = ''
      loc.route(addBase(`/spaces/${r.space_id}`))
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.status === 410)) {
        errorMsg.value = (
          'This invite has expired or already been used. Ask the ' +
          'sender for a fresh one.'
        )
      } else if (e instanceof ApiError && e.status === 403) {
        errorMsg.value = (
          "You're not allowed to join this space (the issuer may have " +
          'revoked the invite).'
        )
      } else {
        errorMsg.value = (e as Error)?.message ?? 'Could not join.'
      }
    } finally {
      submitting.value = false
    }
  }

  if (!open.value) return null

  return (
    <Modal open={true} onClose={close} title="Join a space">
      <div class="sh-join-by-code">
        <p class="sh-muted" style={{ marginTop: 0 }}>
          Paste a code or scan a QR you got from another member.
        </p>

        {/* Two-tile method picker — same class names as PairingFlow's
         *  MethodPicker so the visual + a11y semantics stay in
         *  lockstep with the household pairing dialog. */}
        <div
          class="sh-pairing-method-grid"
          role="tablist"
          aria-label="How to receive the invite"
        >
          <button
            type="button"
            role="tab"
            aria-selected={method.value === 'paste'}
            class={`sh-pairing-method-card ${method.value === 'paste' ? 'sh-pairing-method-card--active' : ''}`}
            onClick={() => pickMethod('paste')}
            data-testid="invite-method-paste"
          >
            <span class="sh-pairing-method-icon" aria-hidden="true">📋</span>
            <span class="sh-pairing-method-title">
              {t('pairing.method_paste')}
            </span>
            <span class="sh-pairing-method-hint">
              {t('pairing.method_paste_hint')}
            </span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={method.value === 'qr'}
            class={`sh-pairing-method-card ${method.value === 'qr' ? 'sh-pairing-method-card--active' : ''}`}
            onClick={() => pickMethod('qr')}
            data-testid="invite-method-qr"
          >
            <span class="sh-pairing-method-icon" aria-hidden="true">📷</span>
            <span class="sh-pairing-method-title">
              {t('pairing.method_qr')}
            </span>
            <span class="sh-pairing-method-hint">
              {t('pairing.method_qr_hint')}
            </span>
          </button>
        </div>

        {method.value === 'paste' && (
          <>
            <textarea
              class="sh-textarea"
              rows={3}
              placeholder="socialhome://invite#…"
              value={draft.value}
              onInput={(e) => {
                draft.value = (e.target as HTMLTextAreaElement).value
                if (errorMsg.value) errorMsg.value = null
              }}
              aria-label="Invite code"
              data-testid="join-by-code-input"
              autoFocus
            />
            {errorMsg.value && (
              <p class="sh-scan-error-inline" role="alert">
                {errorMsg.value}
              </p>
            )}
            <div class="sh-pairing-actions">
              <Button
                onClick={() => void submit()}
                loading={submitting.value}
                disabled={!draft.value.trim()}
              >
                Join
              </Button>
            </div>
          </>
        )}

        {method.value === 'qr' && (
          <>
            <QrScanner
              onPayload={(raw) => {
                void submit(raw)
              }}
              onError={(msg) => { errorMsg.value = msg }}
            />
            {errorMsg.value && (
              <p class="sh-scan-error-inline" role="alert">
                {errorMsg.value}
              </p>
            )}
          </>
        )}
      </div>
    </Modal>
  )
}
