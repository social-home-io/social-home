/**
 * SpaceSubHeader — sticky strip directly below the global TopBar that
 * holds the space's tab nav plus a compact identity badge (small
 * avatar + member count) and trailing action slot (settings button,
 * notif prefs menu).
 *
 * Visual style mirrors the household feed's warm cream surfaces:
 * `--sh-bg-tertiary` background, `--sh-border` hairline, pill-shaped
 * tabs with a terracotta accent on the active one. The strip
 * ``position: sticky``-stacks under the TopBar via `--sh-topbar-height`.
 *
 * Purely presentational: the page owns the ``activeTab`` signal and
 * the data-loading callback. We only render and dispatch.
 *
 * On mobile (or any viewport where the strip overflows horizontally),
 * a ⋯ More button appears next to the actions slot and opens a
 * vertical popover listing every tab. The active tab is also
 * auto-scrolled into view on mount + when it changes, so a member
 * who navigates to Gallery doesn't land on a strip showing only Feed.
 */
import type { Signal } from '@preact/signals'
import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks'
import { Avatar } from './Avatar'

export type SpaceTab =
  | 'feed'
  | 'members'
  | 'pages'
  | 'calendar'
  | 'tasks'
  | 'gallery'
  | 'map'
  | 'moderation'

interface SpaceSubHeaderProps {
  name: string
  emoji: string | null
  coverUrl: string | null
  memberCount: number | null
  activeTab: Signal<SpaceTab>
  visibleTabs: readonly SpaceTab[]
  onSelectTab: (tab: SpaceTab) => void
  /** Optional trailing slot — settings button, notif prefs menu, etc. */
  actions?: preact.ComponentChildren
}

function tabLabel(tab: SpaceTab): string {
  return tab.charAt(0).toUpperCase() + tab.slice(1)
}

export function SpaceSubHeader({
  name, emoji, coverUrl, memberCount,
  activeTab, visibleTabs, onSelectTab, actions,
}: SpaceSubHeaderProps) {
  const stripRef = useRef<HTMLElement | null>(null)
  const overflowWrapRef = useRef<HTMLDivElement | null>(null)
  const menuRef = useRef<HTMLDivElement | null>(null)
  const [overflowing, setOverflowing] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)

  // Detect whether the tab strip can fit every tab without horizontal
  // scrolling. The check fires on mount, every time the tab set or
  // viewport changes, and after fonts settle. When the strip overflows
  // we surface a ⋯ More button next to the actions so members never
  // have to guess that Gallery / Tasks / Moderation exist beyond the
  // fade-mask.
  useLayoutEffect(() => {
    const el = stripRef.current
    if (!el) return
    const check = () => setOverflowing(el.scrollWidth - el.clientWidth > 1)
    check()
    const ro = new ResizeObserver(check)
    ro.observe(el)
    window.addEventListener('resize', check)
    return () => {
      ro.disconnect()
      window.removeEventListener('resize', check)
    }
  }, [visibleTabs])

  // Pin the active tab into view whenever it changes so a member who
  // landed deep in the nav doesn't see a strip scrolled to the start.
  useEffect(() => {
    const el = stripRef.current
    if (!el) return
    const active = el.querySelector('[aria-selected="true"]') as HTMLElement | null
    if (!active) return
    active.scrollIntoView({ behavior: 'auto', inline: 'center', block: 'nearest' })
  }, [activeTab.value])

  // Click-outside + keyboard handling for the overflow menu. Standard
  // ARIA menu pattern: Escape closes, ArrowUp/Down cycle through
  // items, Home/End jump to the ends, Enter activates the focused
  // item. The first time the menu opens we focus the active tab (or
  // the first item) so screen-reader + keyboard-only users land on
  // a known place.
  useEffect(() => {
    if (!menuOpen) return
    const items = (): HTMLButtonElement[] =>
      Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>(
        'button[role="menuitemradio"]',
      ) ?? [])
    // Move focus to the active item — or the first one if there's no
    // active match — once the panel is in the DOM.
    requestAnimationFrame(() => {
      const all = items()
      const target = all.find((i) => i.getAttribute('aria-checked') === 'true')
        ?? all[0]
      target?.focus()
    })
    const onClick = (e: MouseEvent) => {
      if (!overflowWrapRef.current?.contains(e.target as Node)) setMenuOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setMenuOpen(false)
        // Return focus to the trigger so the user keeps their place
        // in the tab order.
        const trigger = overflowWrapRef.current?.querySelector<HTMLButtonElement>(
          '.sh-space-tabs-overflow__trigger',
        )
        trigger?.focus()
        return
      }
      const all = items()
      if (all.length === 0) return
      const current = document.activeElement as HTMLElement | null
      const idx = all.findIndex((i) => i === current)
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        all[(idx + 1 + all.length) % all.length].focus()
      } else if (e.key === 'ArrowUp') {
        e.preventDefault()
        all[(idx - 1 + all.length) % all.length].focus()
      } else if (e.key === 'Home') {
        e.preventDefault()
        all[0].focus()
      } else if (e.key === 'End') {
        e.preventDefault()
        all[all.length - 1].focus()
      }
    }
    document.addEventListener('click', onClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('click', onClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [menuOpen])

  const choose = (tab: SpaceTab) => {
    onSelectTab(tab)
    setMenuOpen(false)
  }

  return (
    <div class="sh-space-subheader" role="presentation">
      <div class="sh-space-subheader-identity">
        <Avatar src={coverUrl} name={emoji ? `${emoji} ${name}` : name} size={28} />
        {memberCount !== null && (
          <span class="sh-space-subheader-meta">
            {memberCount} {memberCount === 1 ? 'member' : 'members'}
          </span>
        )}
      </div>
      <nav
        ref={stripRef}
        class="sh-space-tabs"
        role="tablist"
        aria-label="Space sections"
      >
        {visibleTabs.map(tab => (
          <button
            key={tab}
            type="button"
            role="tab"
            aria-selected={activeTab.value === tab}
            class={
              activeTab.value === tab
                ? 'sh-tab sh-tab--active'
                : 'sh-tab'
            }
            onClick={() => onSelectTab(tab)}
          >
            {tabLabel(tab)}
          </button>
        ))}
      </nav>
      {overflowing && (
        <div class="sh-space-tabs-overflow" ref={overflowWrapRef}>
          <button
            type="button"
            class="sh-space-tabs-overflow__trigger"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            aria-label="More sections"
            title="More sections"
            onClick={() => setMenuOpen(o => !o)}
          >
            <span aria-hidden="true">⋯</span>
          </button>
          {menuOpen && (
            <div
              ref={menuRef}
              class="sh-space-tabs-overflow__panel"
              role="menu"
              aria-label="All sections"
            >
              {visibleTabs.map(tab => (
                <button
                  key={tab}
                  type="button"
                  role="menuitemradio"
                  aria-checked={activeTab.value === tab}
                  tabIndex={activeTab.value === tab ? 0 : -1}
                  class={activeTab.value === tab
                    ? 'sh-space-tabs-overflow__item sh-space-tabs-overflow__item--active'
                    : 'sh-space-tabs-overflow__item'}
                  onClick={() => choose(tab)}
                >
                  <span class="sh-space-tabs-overflow__label">
                    {tabLabel(tab)}
                  </span>
                  {activeTab.value === tab && (
                    <span class="sh-space-tabs-overflow__check" aria-hidden="true">
                      ✓
                    </span>
                  )}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
      {actions && (
        <div class="sh-space-subheader-actions">{actions}</div>
      )}
    </div>
  )
}
