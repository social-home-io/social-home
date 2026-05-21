/**
 * SpaceJoinByCodeCard — paste-a-code or scan-a-QR card on the Spaces
 * dashboard.
 *
 * The receiver pastes a ``socialhome://invite#…`` code (or a bare
 * token, or raw JSON — see :mod:`spaceInviteCode`) and joins the
 * space without ever clicking a deep link. Mirrors the §11 pairing
 * scanner so the muscle memory transfers.
 *
 * Three distinct failure modes get three distinct messages — see the
 * UX-review notes in the slice C plan for why "Could not join"
 * blanket copy is the wrong call here.
 */
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api, ApiError } from '@/api'
import { addBase } from '@/baseUrl'
import { decodeInviteCode } from '@/lib/spaceInviteCode'
import { Button } from '@/components/Button'
import { QrScanner } from '@/components/QrScanner'

const draft = signal('')
const showScanner = signal(false)
const submitting = signal(false)
const errorMsg = signal<string | null>(null)

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

export function SpaceJoinByCodeCard() {
  const loc = useLocation()

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
      draft.value = ''
      showScanner.value = false
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

  if (showScanner.value) {
    return (
      <section class="sh-join-by-code">
        <h3>Scan an invite QR</h3>
        <QrScanner
          onPayload={(raw) => {
            showScanner.value = false
            void submit(raw)
          }}
          onError={(msg) => { errorMsg.value = msg }}
          onCancel={() => { showScanner.value = false }}
        />
        {errorMsg.value && (
          <p class="sh-error" role="alert">{errorMsg.value}</p>
        )}
      </section>
    )
  }

  return (
    <section class="sh-join-by-code">
      <h3>Join with invite code</h3>
      <p class="sh-muted">
        Paste a code or scan a QR you got from another member.
      </p>
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
      />
      {errorMsg.value && (
        <p class="sh-error" role="alert">{errorMsg.value}</p>
      )}
      <div class="sh-form-actions">
        <Button
          variant="secondary"
          onClick={() => { errorMsg.value = null; showScanner.value = true }}
        >
          📷 Scan QR
        </Button>
        <Button
          onClick={() => void submit()}
          loading={submitting.value}
          disabled={!draft.value.trim()}
        >
          Join
        </Button>
      </div>
    </section>
  )
}
