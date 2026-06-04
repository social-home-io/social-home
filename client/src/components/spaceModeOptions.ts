/**
 * Shared option lists for the space visibility + join-mode radio-card groups
 * (used by SpaceCreateDialog and SpaceSettings).
 */
import type { RadioCardOption } from './RadioCardGroup'

// Public is intentionally absent from VISIBILITY_OPTIONS: a public space
// requires a map location the create dialog doesn't collect (the backend
// 422s without it). Public is reached by publishing an existing space from
// its settings.
export const VISIBILITY_OPTIONS: RadioCardOption[] = [
  {
    value: 'private',
    icon: '🔒',
    title: 'Private',
    subtitle: 'Hidden — only people you invite can see it.',
  },
  {
    value: 'household',
    icon: '🏠',
    title: 'Household',
    subtitle: 'Everyone in your home is a member automatically.',
  },
]

export const JOIN_MODE_OPTIONS: RadioCardOption[] = [
  {
    value: 'invite_only',
    icon: '✉️',
    title: 'Invite only',
    subtitle: 'You send invites.',
  },
  {
    value: 'request',
    icon: '🙋',
    title: 'Request to join',
    subtitle: 'People ask; an admin approves.',
  },
  {
    value: 'open',
    icon: '🔓',
    title: 'Open',
    subtitle: 'Anyone can join instantly.',
  },
]
