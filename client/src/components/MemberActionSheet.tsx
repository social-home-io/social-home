/**
 * MemberActionSheet — role/ban actions on space members (§23.98).
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Modal } from './Modal'
import { Button } from './Button'
import { ConfirmDialog } from './ConfirmDialog'
import { showToast } from './Toast'

const open = signal(false)
const memberUserId = signal('')
const memberRole = signal('')
const memberInstanceId = signal<string | null>(null)
const spaceId = signal('')
const showBanConfirm = signal(false)

export function openMemberActions(
  sid: string,
  userId: string,
  role: string,
  instanceId: string | null = null,
) {
  spaceId.value = sid
  memberUserId.value = userId
  memberRole.value = role
  memberInstanceId.value = instanceId
  open.value = true
}

export function MemberActionSheet({ onUpdate }: { onUpdate: () => void }) {
  const setRole = async (role: string) => {
    try {
      const path = memberInstanceId.value
        // #114: a remote member's role lives in space_remote_members,
        // not space_members — the host-only PATCH endpoint targets the
        // ``(instance_id, user_id)`` composite key.
        ? `/api/spaces/${spaceId.value}/remote-members/${memberInstanceId.value}/${memberUserId.value}`
        : `/api/spaces/${spaceId.value}/members/${memberUserId.value}`
      await api.patch(path, { role })
      showToast(`Role changed to ${role}`, 'success')
      open.value = false; onUpdate()
    } catch (e: any) { showToast(e.message || 'Failed', 'error') }
  }

  const ban = async () => {
    try {
      await api.post(`/api/spaces/${spaceId.value}/ban`, { user_id: memberUserId.value })
      showToast('Member banned', 'info')
      showBanConfirm.value = false; open.value = false; onUpdate()
    } catch (e: any) { showToast(e.message || 'Failed', 'error') }
  }

  const remove = async () => {
    try {
      const path = memberInstanceId.value
        // #114 phase 2: a remote member kick goes to the dedicated
        // endpoint so the host can route via SPACE_REMOTE_MEMBER_REMOVED
        // + epoch rotation. Cross-household admin kicking a local
        // member from a remote space goes through the regular
        // /members/ endpoint, where SpaceService.remove_member detects
        // the remote-host case and federates SPACE_REMOTE_ADMIN_KICK.
        ? `/api/spaces/${spaceId.value}/remote-members/${memberInstanceId.value}/${memberUserId.value}`
        : `/api/spaces/${spaceId.value}/members/${memberUserId.value}`
      await api.delete(path)
      showToast('Member removed', 'info')
      open.value = false; onUpdate()
    } catch (e: any) { showToast(e.message || 'Failed', 'error') }
  }

  // Ban only meaningful on the host side, and only for local members.
  // Remote members are kicked via SPACE_REMOTE_MEMBER_REMOVED instead.
  const canBan = !memberInstanceId.value

  return (
    <>
      <Modal open={open.value} onClose={() => open.value = false} title="Member Actions">
        <div class="sh-member-actions">
          {memberRole.value !== 'admin' && (
            <Button variant="secondary" onClick={() => setRole('admin')}>Promote to admin</Button>
          )}
          {memberRole.value === 'admin' && (
            <Button variant="secondary" onClick={() => setRole('member')}>Demote to member</Button>
          )}
          <Button variant="secondary" onClick={remove}>Remove from space</Button>
          {canBan && (
            <Button variant="danger" onClick={() => showBanConfirm.value = true}>Ban</Button>
          )}
        </div>
      </Modal>
      <ConfirmDialog open={showBanConfirm.value} title="Ban member?"
        message="This member will be removed and cannot rejoin until unbanned."
        confirmLabel="Ban" destructive onConfirm={ban}
        onCancel={() => showBanConfirm.value = false} />
    </>
  )
}
