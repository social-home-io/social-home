/**
 * UserActionsMenu — overflow menu for user-targeted actions (§Privacy).
 *
 * v1 surfaces a single action: **Block** the user. The menu is opened
 * via :func:`openUserActions` and rendered once in the app shell next
 * to the other global dialogs.
 *
 * Once a block lands the global :func:`blockedUserIds` signal updates,
 * so any list / ring that reads that signal hides the row immediately
 * — the server's repo-layer filter is already authoritative; this is
 * just the optimistic UI sync. The blocker is offered an Unblock
 * shortcut from Settings → Privacy → Blocked accounts.
 */
import { signal } from '@preact/signals'
import { Modal } from './Modal'
import { Button } from './Button'
import { ConfirmDialog } from './ConfirmDialog'
import { showToast } from './Toast'
import { blockUser, unblockUser, isBlocked } from '@/store/blocks'
import {
  followUser,
  unfollowUser,
  isFollowing,
  loadFollows,
} from '@/store/follows'

const open = signal(false)
const targetUserId = signal('')
const targetDisplayName = signal('')
const showConfirmBlock = signal(false)

/** Open the menu against ``user_id`` / ``display_name``.  ``display_name``
 *  is rendered in the confirm body — fall back to the user_id if you
 *  don't have a friendlier label handy. */
export function openUserActions(userId: string, displayName?: string): void {
  targetUserId.value = userId
  targetDisplayName.value = displayName?.trim() || userId
  showConfirmBlock.value = false
  open.value = true
  // Hydrate the follow cache so the Follow / Unfollow toggle reflects
  // the current state when the modal opens.
  void loadFollows()
}

export function UserActionsMenu() {
  const alreadyBlocked = isBlocked(targetUserId.value)
  const alreadyFollowing = isFollowing(targetUserId.value)

  const onBlock = async () => {
    try {
      await blockUser(targetUserId.value)
      showToast(`Blocked ${targetDisplayName.value}`, 'success')
      showConfirmBlock.value = false
      open.value = false
    } catch (e: unknown) {
      showToast(`Couldn't block: ${(e as Error)?.message ?? e}`, 'error')
    }
  }

  const onUnblock = async () => {
    try {
      await unblockUser(targetUserId.value)
      showToast(`Unblocked ${targetDisplayName.value}`, 'success')
      open.value = false
    } catch (e: unknown) {
      showToast(`Couldn't unblock: ${(e as Error)?.message ?? e}`, 'error')
    }
  }

  const onFollow = async () => {
    try {
      await followUser(targetUserId.value)
      showToast(`Following ${targetDisplayName.value}`, 'success')
      open.value = false
    } catch (e: unknown) {
      showToast(`Couldn't follow: ${(e as Error)?.message ?? e}`, 'error')
    }
  }

  const onUnfollow = async () => {
    try {
      await unfollowUser(targetUserId.value)
      showToast(`Unfollowed ${targetDisplayName.value}`, 'success')
      open.value = false
    } catch (e: unknown) {
      showToast(`Couldn't unfollow: ${(e as Error)?.message ?? e}`, 'error')
    }
  }

  return (
    <>
      <Modal
        open={open.value}
        onClose={() => { open.value = false }}
        title={targetDisplayName.value}
      >
        <div class="sh-user-actions">
          {!alreadyBlocked && !alreadyFollowing && (
            <Button variant="primary" onClick={onFollow}>
              ➕ Follow
            </Button>
          )}
          {!alreadyBlocked && alreadyFollowing && (
            <Button variant="secondary" onClick={onUnfollow}>
              ✓ Unfollow
            </Button>
          )}
          {!alreadyBlocked && (
            <Button
              variant="danger"
              onClick={() => { showConfirmBlock.value = true }}
            >
              🚫 Block this user
            </Button>
          )}
          {alreadyBlocked && (
            <Button variant="secondary" onClick={onUnblock}>
              ✓ Unblock
            </Button>
          )}
          <Button variant="ghost" onClick={() => { open.value = false }}>
            Cancel
          </Button>
        </div>
      </Modal>
      <ConfirmDialog
        open={showConfirmBlock.value}
        title={`Block ${targetDisplayName.value}?`}
        message={
          `You won't see their highlights, posts, presence, or friends-list `
          + `entry, and neither of you can DM the other. You can unblock `
          + `anytime in Settings → Privacy → Blocked accounts.`
        }
        confirmLabel="Block"
        destructive
        onConfirm={onBlock}
        onCancel={() => { showConfirmBlock.value = false }}
      />
    </>
  )
}
