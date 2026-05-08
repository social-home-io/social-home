import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { LocationProvider } from 'preact-iso'
import { MobileNav } from './MobileNav'

const wrap = (ui: any) => (
  <LocationProvider>{ui}</LocationProvider>
)

describe('MobileNav', () => {
  it('renders 5 bottom tabs (4 link tabs + 1 More button)', () => {
    const { container } = render(wrap(<MobileNav />))
    // Only the bottom-bar tabs — the drawer hosts the full SideNav,
    // whose many links would otherwise inflate the count.
    const bar = container.querySelector('.sh-mobile-nav')!
    const tabs = bar.querySelectorAll('.sh-mobile-tab')
    expect(tabs.length).toBe(5)
    // 4 are anchor links, 1 is a button (the "More" toggle).
    expect(bar.querySelectorAll('a.sh-mobile-tab').length).toBe(4)
    expect(bar.querySelectorAll('button.sh-mobile-tab').length).toBe(1)
  })

  it('has navigation role', () => {
    const { container } = render(wrap(<MobileNav />))
    expect(container.querySelector('nav[role="navigation"]')).toBeTruthy()
  })

  it('contains Home and Spaces tabs', () => {
    const { container } = render(wrap(<MobileNav />))
    const bar = container.querySelector('.sh-mobile-nav')!
    expect(bar.textContent).toContain('Home')
    expect(bar.textContent).toContain('Spaces')
  })

  it('marks the matching tab active via aria-current + sh-active', () => {
    // jsdom navigates to "/" by default, so the Feed tab should be the
    // active one without any extra setup.
    const { container } = render(wrap(<MobileNav />))
    const bar = container.querySelector('.sh-mobile-nav')!
    const active = bar.querySelector('a.sh-active') as HTMLAnchorElement | null
    expect(active).toBeTruthy()
    expect(active?.getAttribute('aria-current')).toBe('page')
    expect(active?.getAttribute('href')).toBe('/')
  })

  it('More tab toggles the sidebar drawer', () => {
    const { container, rerender } = render(wrap(<MobileNav />))
    const drawer = container.querySelector('.sh-mobile-drawer')!
    expect(drawer.classList.contains('sh-mobile-drawer--open')).toBe(false)
    const more = container.querySelector('button.sh-mobile-tab') as HTMLButtonElement
    more.click()
    rerender(wrap(<MobileNav />))
    expect(
      container.querySelector('.sh-mobile-drawer')!.classList.contains('sh-mobile-drawer--open'),
    ).toBe(true)
  })
})
