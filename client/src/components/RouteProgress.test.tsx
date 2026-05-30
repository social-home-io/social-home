import { describe, it, expect, beforeEach } from 'vitest'
import { render, act } from '@testing-library/preact'
import { RouteProgress, routeLoadStart, routeLoadEnd } from './RouteProgress'

describe('RouteProgress', () => {
  beforeEach(() => {
    // Drain any in-flight count left by a previous test.
    routeLoadEnd()
    routeLoadEnd()
  })

  it('renders nothing when no route load is in flight', () => {
    const { container } = render(<RouteProgress />)
    expect(container.querySelector('.sh-route-progress')).toBeNull()
  })

  it('shows the bar while a load is in flight and hides it after it ends', () => {
    const { container } = render(<RouteProgress />)
    act(() => { routeLoadStart() })
    expect(container.querySelector('.sh-route-progress')).toBeTruthy()
    expect(
      container.querySelector('.sh-route-progress')?.getAttribute('aria-busy'),
    ).toBe('true')
    act(() => { routeLoadEnd() })
    expect(container.querySelector('.sh-route-progress')).toBeNull()
  })

  it('stays visible until every overlapping load finishes', () => {
    const { container } = render(<RouteProgress />)
    act(() => { routeLoadStart(); routeLoadStart() })
    expect(container.querySelector('.sh-route-progress')).toBeTruthy()
    act(() => { routeLoadEnd() })
    // One load still in flight — bar stays.
    expect(container.querySelector('.sh-route-progress')).toBeTruthy()
    act(() => { routeLoadEnd() })
    expect(container.querySelector('.sh-route-progress')).toBeNull()
  })

  it('never wedges negative on a stray end', () => {
    const { container } = render(<RouteProgress />)
    act(() => { routeLoadEnd() })
    expect(container.querySelector('.sh-route-progress')).toBeNull()
    act(() => { routeLoadStart() })
    expect(container.querySelector('.sh-route-progress')).toBeTruthy()
    act(() => { routeLoadEnd() })
    expect(container.querySelector('.sh-route-progress')).toBeNull()
  })
})
