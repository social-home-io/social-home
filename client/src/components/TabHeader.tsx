/**
 * TabHeader — generic tab strip used by single-page surfaces that
 * need an in-page tab nav (DM panel, future Bazaar / Calendar
 * sub-views, etc.).
 *
 * Mirrors the visual treatment of :class:`SpaceSubHeader` (terracotta
 * accent on the active tab, pill-shaped buttons, sticky under the
 * TopBar) without the space-specific avatar / member-count chrome.
 * Purely presentational — the host page owns tab state.
 */
import type { ComponentChildren } from 'preact'

interface TabHeaderProps<T extends string> {
  /** Identifier of the tab the page treats as active. */
  activeTab: T
  /** Tabs to render, in display order. Filter externally if a tab
   *  should be hidden for the current user. */
  visibleTabs: readonly T[]
  /** Display label per tab — keeps the component free of i18n
   *  knowledge so callers can pass localised strings. */
  labels: Readonly<Record<T, string>>
  onSelectTab: (tab: T) => void
  /** Accessible name for the ``<nav role="tablist">`` element. */
  ariaLabel: string
  /** Optional trailing slot — header action buttons, badges, etc. */
  actions?: ComponentChildren
}


export function TabHeader<T extends string>({
  activeTab,
  visibleTabs,
  labels,
  onSelectTab,
  ariaLabel,
  actions,
}: TabHeaderProps<T>) {
  return (
    <div class="sh-space-subheader" role="presentation">
      <nav class="sh-space-tabs" role="tablist" aria-label={ariaLabel}>
        {visibleTabs.map((tab) => (
          <button
            key={tab}
            type="button"
            role="tab"
            aria-selected={activeTab === tab}
            class={
              activeTab === tab
                ? 'sh-tab sh-tab--active'
                : 'sh-tab'
            }
            onClick={() => onSelectTab(tab)}
          >
            {labels[tab]}
          </button>
        ))}
      </nav>
      {actions && (
        <div class="sh-space-subheader-actions">{actions}</div>
      )}
    </div>
  )
}
