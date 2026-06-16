/**
 * ProfileCard — user profile popup (§23.40).
 */
import { Avatar } from './Avatar'
import { Button } from './Button'
import type { User } from '@/types'

interface ProfileCardProps {
  user: User
  onDm?: () => void
  onClose: () => void
}

export function ProfileCard({ user, onDm, onClose }: ProfileCardProps) {
  return (
    <div class="sh-profile-card" onClick={(e) => e.stopPropagation()}>
      <button
        type="button"
        class="sh-profile-close"
        aria-label="Close profile"
        onClick={onClose}
      >✕</button>
      <div class="sh-profile-header">
        <Avatar name={user.display_name} src={user.picture_url} size={64} />
        <h3>{user.display_name}</h3>
        <span class="sh-muted">@{user.handle ?? user.username}</span>
        {user.is_admin && <span class="sh-badge sh-badge--admin">Admin</span>}
      </div>
      {user.bio && <p class="sh-profile-bio">{user.bio}</p>}
      <div class="sh-profile-actions">
        {onDm && <Button onClick={onDm}>Message</Button>}
      </div>
    </div>
  )
}
