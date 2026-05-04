import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { LocationProvider } from 'preact-iso'
import { MobileNav } from './MobileNav'

const wrap = (ui: any) => (
  <LocationProvider>{ui}</LocationProvider>
)

describe('MobileNav', () => {
  it('renders tab links', () => {
    const { container } = render(wrap(<MobileNav />))
    const links = container.querySelectorAll('a')
    expect(links.length).toBe(5)
  })

  it('has navigation role', () => {
    const { container } = render(wrap(<MobileNav />))
    expect(container.querySelector('[role="navigation"]')).toBeTruthy()
  })

  it('contains Feed and Spaces tabs', () => {
    const { container } = render(wrap(<MobileNav />))
    expect(container.textContent).toContain('Feed')
    expect(container.textContent).toContain('Spaces')
  })

  it('marks the matching tab active via aria-current + sh-active', () => {
    // jsdom navigates to "/" by default, so the Feed tab should be the
    // active one without any extra setup.
    const { container } = render(wrap(<MobileNav />))
    const active = container.querySelector('a.sh-active')
    expect(active).toBeTruthy()
    expect(active?.getAttribute('aria-current')).toBe('page')
    expect(active?.getAttribute('href')).toBe('/')
  })
})
