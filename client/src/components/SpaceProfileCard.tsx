/**
 * SpaceProfileCard — public space preview (§23.110, §23.102).
 */
import { Button } from './Button'
import { JoinSpaceButton } from './SpaceJoinLeave'

interface SpaceProfile {
  space_id: string; name: string; description?: string; emoji?: string
  member_count: number; join_mode: string
}

export function SpaceProfileCard({ space, onClose }: {
  space: SpaceProfile; onClose: () => void
}) {
  return (
    <div class="sh-space-profile" onClick={(e) => e.stopPropagation()}>
      <button
        type="button"
        class="sh-profile-close"
        aria-label="Close space profile"
        onClick={onClose}
      >✕</button>
      <div class="sh-space-profile-header">
        <span class="sh-space-emoji-lg">{space.emoji || '🌐'}</span>
        <h3>{space.name}</h3>
        <span class="sh-muted">{space.member_count} members</span>
      </div>
      {space.description && <p class="sh-space-profile-desc">{space.description}</p>}
      <div class="sh-space-profile-actions">
        <JoinSpaceButton spaceId={space.space_id} joinMode={space.join_mode} />
        <Button variant="secondary" onClick={() => {
          // v1: report flow
          import('./ReportDialog').then(m => m.openReport('space', space.space_id))
        }}>Report</Button>
      </div>
    </div>
  )
}

/**
 * PublicSpaceFlagWarning — flagging + federation warning (§23.102).
 */
export function PublicSpaceFlagWarning({ instanceId }: { instanceId: string }) {
  return (
    <div class="sh-flag-warning" role="alert">
      <strong>⚠️ External content</strong>
      <p class="sh-muted">This space is hosted on a different instance ({instanceId}). Content is subject to their moderation policies.</p>
    </div>
  )
}
