/**
 * GroupDmHeader — group DM header + member management (§23.47c).
 */
import { useSignal } from '@preact/signals'
import { api } from '@/api'
import { Avatar } from './Avatar'
import { Button } from './Button'
import { showToast } from './Toast'

interface GroupMember { username: string; display_name?: string }

export function GroupDmHeader({ conversationId, name, members, onUpdate }: {
  conversationId: string; name: string | null
  members: GroupMember[]; onUpdate: () => void
}) {
  const showMembers = useSignal(false)

  const addMember = async () => {
    const username = prompt('Username to add:')
    if (!username?.trim()) return
    try {
      await api.post(`/api/conversations/${conversationId}/members`, { username })
      showToast('Member added', 'success')
      onUpdate()
    } catch (e: any) { showToast(e.message || 'Failed', 'error') }
  }

  return (
    <div class="sh-group-header">
      <div class="sh-group-title">
        <h2>{name || 'Group'}</h2>
        <span class="sh-muted">{members.length} members</span>
        <button class="sh-link" onClick={() => showMembers.value = !showMembers.value}>
          {showMembers.value ? 'Hide' : 'Members'}
        </button>
      </div>
      {showMembers.value && (
        <div class="sh-group-members">
          {members.map(m => (
            <div key={m.username} class="sh-group-member">
              <Avatar name={m.display_name || m.username} size={24} />
              <span>{m.display_name || m.username}</span>
            </div>
          ))}
          <Button variant="secondary" onClick={addMember}>+ Add member</Button>
        </div>
      )}
    </div>
  )
}
