import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'
import { TabHeader } from './TabHeader'

type DmTab = 'dms' | 'groups' | 'calls'
const LABELS: Record<DmTab, string> = {
  dms: 'DMs',
  groups: 'Groups',
  calls: 'Calls',
}
const TABS: readonly DmTab[] = ['dms', 'groups', 'calls']

function forceOverflow() {
  Object.defineProperty(HTMLElement.prototype, 'scrollWidth', {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList?.contains('sh-space-tabs') ? 9999 : 0
    },
  })
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
    configurable: true,
    get() { return 200 },
  })
}

function restoreLayout() {
  delete (HTMLElement.prototype as unknown as Record<string, unknown>).scrollWidth
  delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientWidth
}

beforeEach(() => {
  restoreLayout()
  ;(window as unknown as Record<string, unknown>).ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  HTMLElement.prototype.scrollIntoView = vi.fn()
})

describe('TabHeader', () => {
  it('renders one tab button per visibleTabs entry, marks the active one selected', () => {
    const { container, getByText } = render(
      <TabHeader<DmTab>
        activeTab="groups"
        visibleTabs={TABS}
        labels={LABELS}
        onSelectTab={() => {}}
        ariaLabel="Chat sections"
      />,
    )
    const buttons = container.querySelectorAll('nav[role="tablist"] button[role="tab"]')
    expect(buttons.length).toBe(TABS.length)
    const active = getByText('Groups').closest('button')!
    expect(active.getAttribute('aria-selected')).toBe('true')
    expect(active.classList.contains('sh-tab--active')).toBe(true)
  })

  it('calls onSelectTab when a tab is clicked', () => {
    const onSelect = vi.fn()
    const { getByText } = render(
      <TabHeader<DmTab>
        activeTab="dms"
        visibleTabs={TABS}
        labels={LABELS}
        onSelectTab={onSelect}
        ariaLabel="Chat sections"
      />,
    )
    fireEvent.click(getByText('Calls'))
    expect(onSelect).toHaveBeenCalledWith('calls')
  })

  it('renders the actions slot when provided', () => {
    const { getByText } = render(
      <TabHeader<DmTab>
        activeTab="dms"
        visibleTabs={TABS}
        labels={LABELS}
        onSelectTab={() => {}}
        ariaLabel="Chat sections"
        actions={<button type="button">+ New message</button>}
      />,
    )
    expect(getByText('+ New message')).toBeTruthy()
  })

  it('hides the ⋯ overflow trigger when the strip fits', () => {
    const { queryByLabelText } = render(
      <TabHeader<DmTab>
        activeTab="dms"
        visibleTabs={TABS}
        labels={LABELS}
        onSelectTab={() => {}}
        ariaLabel="Chat sections"
      />,
    )
    expect(queryByLabelText('More sections')).toBeNull()
  })

  it('renders the ⋯ overflow trigger and popover when the strip overflows', async () => {
    forceOverflow()
    const { findByLabelText, getByRole } = render(
      <TabHeader<DmTab>
        activeTab="dms"
        visibleTabs={TABS}
        labels={LABELS}
        onSelectTab={() => {}}
        ariaLabel="Chat sections"
      />,
    )
    const trigger = await findByLabelText('More sections')
    fireEvent.click(trigger)
    await waitFor(() => {
      expect(trigger.getAttribute('aria-expanded')).toBe('true')
    })
    const menu = getByRole('menu', { name: 'All sections' })
    const items = menu.querySelectorAll('button[role="menuitemradio"]')
    expect(items.length).toBe(TABS.length)
    const dms = Array.from(items).find(
      (el) => el.querySelector('.sh-space-tabs-overflow__label')?.textContent === 'DMs',
    )!
    expect(dms.getAttribute('aria-checked')).toBe('true')
  })

  it('selecting a tab in the popover fires onSelectTab and closes the menu', async () => {
    forceOverflow()
    const onSelect = vi.fn()
    const { findByLabelText, getByRole, queryByRole } = render(
      <TabHeader<DmTab>
        activeTab="dms"
        visibleTabs={TABS}
        labels={LABELS}
        onSelectTab={onSelect}
        ariaLabel="Chat sections"
      />,
    )
    fireEvent.click(await findByLabelText('More sections'))
    const menu = getByRole('menu', { name: 'All sections' })
    const groups = Array.from(
      menu.querySelectorAll('button[role="menuitemradio"]'),
    ).find(
      (el) => el.querySelector('.sh-space-tabs-overflow__label')?.textContent === 'Groups',
    ) as HTMLButtonElement
    fireEvent.click(groups)
    expect(onSelect).toHaveBeenCalledWith('groups')
    await waitFor(() => {
      expect(queryByRole('menu', { name: 'All sections' })).toBeNull()
    })
  })
})
