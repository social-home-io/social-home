/**
 * Shared option lists for the space visibility + join-mode radio-card groups
 * (used by SpaceCreateDialog and SpaceSettings).
 */
import type { RadioCardOption } from './RadioCardGroup'

// Public requires a map location (the create dialog collects one when this
// is chosen; the backend 422s without it).
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
  {
    value: 'public',
    icon: '🌐',
    title: 'Public',
    subtitle: 'Listed on the public map for anyone to discover.',
  },
  {
    value: 'global',
    icon: '✦',
    title: 'Global',
    subtitle: 'Published worldwide via your global server.',
  },
]

/** Discovery categories (§23.50) — shown for public/global spaces. Values
 *  mirror the backend ``SPACE_CATEGORIES`` (socialhome/domain/space.py) and the
 *  GFS label map (global_server/public.py). */
export const SPACE_CATEGORIES: { value: string; label: string }[] = [
  { value: 'general',          label: 'General' },
  { value: 'hobby_crafts',     label: 'Hobby & crafts' },
  { value: 'sports_outdoors',  label: 'Sports & outdoors' },
  { value: 'gaming',           label: 'Gaming' },
  { value: 'music_arts',       label: 'Music & arts' },
  { value: 'food_drink',       label: 'Food & drink' },
  { value: 'tech',             label: 'Tech' },
  { value: 'local',            label: 'Local / neighborhood' },
  { value: 'family_parenting', label: 'Family & parenting' },
  { value: 'learning',         label: 'Learning' },
]

/** Map any value (unknown/legacy/empty/null) to a display label; default General. */
export function categoryLabel(value: string | null | undefined): string {
  return SPACE_CATEGORIES.find(c => c.value === value)?.label ?? 'General'
}

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

/**
 * Join-mode options for a given visibility. A **private** space is
 * invite-only by definition (you can't request or openly join something
 * hidden), so the non-invite options are shown but disabled — the
 * constraint is visible rather than hidden.
 */
export function joinOptionsForVisibility(spaceType: string): RadioCardOption[] {
  if (spaceType !== 'private') return JOIN_MODE_OPTIONS
  return JOIN_MODE_OPTIONS.map((o) =>
    o.value === 'invite_only' ? o : { ...o, disabled: true },
  )
}
