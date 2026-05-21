/**
 * Shared overflow-menu pieces used by both :class:`SpaceSubHeader` and
 * :class:`TabHeader`. Lets either surface re-use the exact same
 * affordance — ResizeObserver-driven overflow detection, ⋯ More button
 * next to the actions slot, vertical popover with ARIA keyboard model,
 * active-tab scrollIntoView — without duplicating ~80 lines of JSX +
 * hooks.
 *
 * The two host components own their own tab-strip <nav> + actions so
 * they can keep their own DOM ordering (the space subheader has an
 * identity badge before the strip; the generic TabHeader doesn't).
 * They pass the strip ref into ``useTabStripOverflow`` to drive the
 * ⋯ button, and render :component:`TabOverflowMenu` next to the
 * actions when overflow is detected.
 */
import type { JSX, RefObject } from 'preact'
import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks'

export function useTabStripOverflow(
  stripRef: RefObject<HTMLElement>,
  // The deps that should re-trigger an overflow re-measure (e.g. the
  // tab list itself — adding a tab can flip overflow on or off).
  deps: ReadonlyArray<unknown>,
): boolean {
  const [overflowing, setOverflowing] = useState(false)
  useLayoutEffect(() => {
    const el = stripRef.current
    if (!el) return
    const check = () => setOverflowing(el.scrollWidth - el.clientWidth > 1)
    check()
    // ``ResizeObserver`` is not in jsdom by default — guard the
    // construction so existing component tests don't have to stub
    // it just to mount the strip. Production browsers (every
    // target Social Home runs on) ship it natively.
    const RO: typeof ResizeObserver | undefined =
      typeof ResizeObserver !== 'undefined' ? ResizeObserver : undefined
    const ro = RO ? new RO(check) : null
    ro?.observe(el)
    window.addEventListener('resize', check)
    return () => {
      ro?.disconnect()
      window.removeEventListener('resize', check)
    }
    // ``stripRef`` itself is stable across renders (the consumer
    // hands us the same ref instance); only the deps the consumer
    // explicitly passes should re-fire the measurement.
  }, deps)
  return overflowing
}

export function useScrollActiveTabIntoView(
  stripRef: RefObject<HTMLElement>,
  activeKey: string,
): void {
  useEffect(() => {
    const el = stripRef.current
    if (!el) return
    const active = el.querySelector('[aria-selected="true"]') as HTMLElement | null
    // ``scrollIntoView`` is missing in jsdom by default — silently
    // skip the pin so component tests don't have to stub it.
    active?.scrollIntoView?.({ behavior: 'auto', inline: 'center', block: 'nearest' })
  }, [activeKey])
}

interface TabOverflowMenuProps<T extends string> {
  visibleTabs: readonly T[]
  activeTab: T
  labels: Readonly<Record<T, string>>
  onSelectTab: (tab: T) => void
}

export function TabOverflowMenu<T extends string>({
  visibleTabs,
  activeTab,
  labels,
  onSelectTab,
}: TabOverflowMenuProps<T>): JSX.Element {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const menuRef = useRef<HTMLDivElement | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)

  // Standard ARIA menu keyboard model: Escape closes (and returns
  // focus to the trigger), Arrow keys cycle, Home/End jump, focus
  // moves to the active item on open.
  useEffect(() => {
    if (!menuOpen) return
    const items = (): HTMLButtonElement[] =>
      Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>(
        'button[role="menuitemradio"]',
      ) ?? [])
    requestAnimationFrame(() => {
      const all = items()
      const target = all.find((i) => i.getAttribute('aria-checked') === 'true')
        ?? all[0]
      target?.focus()
    })
    const onClick = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setMenuOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setMenuOpen(false)
        const trigger = wrapRef.current?.querySelector<HTMLButtonElement>(
          '.sh-space-tabs-overflow__trigger',
        )
        trigger?.focus()
        return
      }
      const all = items()
      if (all.length === 0) return
      const idx = all.findIndex((i) => i === document.activeElement)
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

  const choose = (tab: T) => {
    onSelectTab(tab)
    setMenuOpen(false)
  }

  return (
    <div class="sh-space-tabs-overflow" ref={wrapRef}>
      <button
        type="button"
        class="sh-space-tabs-overflow__trigger"
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        aria-label="More sections"
        title="More sections"
        onClick={() => setMenuOpen((o) => !o)}
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
          {visibleTabs.map((tab) => (
            <button
              key={tab}
              type="button"
              role="menuitemradio"
              aria-checked={activeTab === tab}
              tabIndex={activeTab === tab ? 0 : -1}
              class={activeTab === tab
                ? 'sh-space-tabs-overflow__item sh-space-tabs-overflow__item--active'
                : 'sh-space-tabs-overflow__item'}
              onClick={() => choose(tab)}
            >
              <span class="sh-space-tabs-overflow__label">{labels[tab]}</span>
              {activeTab === tab && (
                <span class="sh-space-tabs-overflow__check" aria-hidden="true">
                  ✓
                </span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
