/**
 * SpaceInviteDialog — create and share invite links to a space
 * (§23.62).
 *
 * Opens via :func:`openSpaceInvite(spaceId)`.  The admin picks how
 * many one-time uses the token should support, generates it, then
 * copies the resulting ``/join?token=…`` URL.  After generation the
 * dialog shows the share-ready link plus a "Make another" button so
 * an admin doesn't have to close + reopen to create a second link.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Modal } from './Modal'
import { Button } from './Button'
import { showToast } from './Toast'

const open = signal(false)
const spaceId = signal('')
const inviteToken = signal('')
const uses = signal(1)
const loading = signal(false)

export function openSpaceInvite(sid: string) {
  spaceId.value = sid
  inviteToken.value = ''
  uses.value = 1
  open.value = true
}

export function SpaceInviteDialog() {
  const link = inviteToken.value
    ? `${location.origin}/join?token=${inviteToken.value}`
    : ''

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

  const copyLink = async () => {
    try {
      await navigator.clipboard.writeText(link)
      showToast('Invite link copied!', 'success')
    } catch {
      showToast('Could not copy — select the link to copy manually.', 'error')
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
        {!inviteToken.value ? (
          <>
            <p class="sh-muted" style={{ marginTop: 0 }}>
              Generate a one-time link your invitee opens to join. Anyone
              with the link can join the space until it's used up.
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
                Generate invite link
              </Button>
            </div>
          </>
        ) : (
          <>
            <p class="sh-muted" style={{ marginTop: 0 }}>
              Share this link — once tapped, your invitee joins
              automatically.
            </p>
            {/* The link is the share-ready URL; the bare token is
             *  hidden because nobody actually needs to read it. */}
            <div class="sh-invite-link-row">
              <code class="sh-invite-link">{link}</code>
            </div>
            <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
              Good for {uses.value} {uses.value === 1 ? 'use' : 'uses'}.
            </p>
            <div class="sh-form-actions">
              <Button variant="secondary" onClick={reset}>
                Make another
              </Button>
              <Button onClick={copyLink}>Copy link</Button>
            </div>
          </>
        )}
      </div>
    </Modal>
  )
}
