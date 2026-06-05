/**
 * SpaceJoinLeave — join and leave flows (§23.101, §23.68).
 */
import { signal, useSignal } from '@preact/signals'
import { api } from '@/api'
import { Button } from './Button'
import { ConfirmDialog } from './ConfirmDialog'
import { showToast } from './Toast'

const showLeave = signal(false)
const leaving = signal(false)

export function JoinSpaceButton({ spaceId, joinMode }: {
  spaceId: string; joinMode: string
}) {
  const joining = useSignal(false)

  const join = async () => {
    joining.value = true
    try {
      if (joinMode === 'open') {
        await api.post(`/api/spaces/${spaceId}/join`)
        showToast('Joined space!', 'success')
      } else if (joinMode === 'request') {
        const message = prompt('Message to admins (optional):')
        await api.post(`/api/spaces/${spaceId}/join-request`, { message })
        showToast('Join request sent', 'success')
      } else {
        showToast('This space requires an invite link', 'info')
      }
    } catch (e: any) { showToast(e.message || 'Failed', 'error') }
    finally { joining.value = false }
  }

  return (
    <Button onClick={join} loading={joining.value}>
      {joinMode === 'request' ? 'Request to join' : 'Join'}
    </Button>
  )
}

export function LeaveSpaceButton({ spaceId, onLeft }: {
  spaceId: string; onLeft?: () => void
}) {
  const leave = async () => {
    leaving.value = true
    try {
      await api.delete(`/api/spaces/${spaceId}/members/me`)
      showToast('Left space', 'info')
      showLeave.value = false
      onLeft?.()
    } catch (e: any) { showToast(e.message || 'Failed', 'error') }
    finally { leaving.value = false }
  }

  return (
    <>
      <Button variant="danger" onClick={() => showLeave.value = true}>Leave space</Button>
      <ConfirmDialog open={showLeave.value} title="Leave this space?"
        message="You will lose access to the space's content. You can rejoin if invited."
        confirmLabel="Leave" destructive onConfirm={leave}
        onCancel={() => showLeave.value = false} />
    </>
  )
}
