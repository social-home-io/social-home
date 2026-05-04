/**
 * MobileNav — bottom tab bar for mobile (§23.20).
 * Only visible below the responsive breakpoint.
 *
 * Marks the active tab with both ``aria-current="page"`` (assistive
 * tech) and the ``sh-active`` class (visual). Without the active state
 * the bar gives no orientation cue, so a mobile user couldn't tell at
 * a glance which tab they were on.
 */
import { useLocation } from 'preact-iso'

interface Tab {
  href:   string
  emoji:  string
  label:  string
  /** Match function — handles "is the user on this tab or a descendant?".
   *  Default is exact match against the path. */
  matches?: (path: string) => boolean
}

const TABS: readonly Tab[] = [
  { href: '/',              emoji: '🏠', label: 'Feed',
    matches: (p) => p === '/' },
  { href: '/spaces',        emoji: '💬', label: 'Spaces',
    matches: (p) => p === '/spaces' || p.startsWith('/spaces/') },
  { href: '/dms',           emoji: '✉️', label: 'DMs',
    matches: (p) => p === '/dms' || p.startsWith('/dms/') },
  { href: '/notifications', emoji: '🔔', label: 'Notifs',
    matches: (p) => p.startsWith('/notifications') },
  { href: '/settings',      emoji: '⚙️', label: 'More',
    matches: (p) => p.startsWith('/settings') },
]

export function MobileNav() {
  const loc = useLocation()
  const path = loc.path
  return (
    <nav class="sh-mobile-nav" role="navigation" aria-label="Mobile navigation">
      {TABS.map((t) => {
        const active = t.matches ? t.matches(path) : path === t.href
        return (
          <a
            key={t.href}
            href={t.href}
            class={`sh-mobile-tab${active ? ' sh-active' : ''}`}
            aria-current={active ? 'page' : undefined}
          >
            {t.emoji}<span>{t.label}</span>
          </a>
        )
      })}
    </nav>
  )
}
