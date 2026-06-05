import { describe, it, expect, beforeEach } from 'vitest'
import { render, act } from '@testing-library/preact'
import { RouteProgress, routeLoadStart, routeLoadEnd } from './RouteProgress'

// `routeLoadStart` / `routeLoadEnd` defer their signal write to a microtask
// (see RouteProgress.tsx — the #473 suspend-loop fix), so every assertion has
// to flush microtasks before reading the bar. `flush()` wraps the drain in
// `act` so the signal-driven re-render is applied.
const flush = () => act(async () => { await Promise.resolve() })

describe('RouteProgress', () => {
  beforeEach(async () => {
    // Drain any in-flight count left by a previous test.
    routeLoadEnd()
    routeLoadEnd()
    await flush()
  })

  it('renders nothing when no route load is in flight', () => {
    const { container } = render(<RouteProgress />)
    expect(container.querySelector('.sh-route-progress')).toBeNull()
  })

  it('shows the bar while a load is in flight and hides it after it ends', async () => {
    const { container } = render(<RouteProgress />)
    routeLoadStart()
    await flush()
    expect(container.querySelector('.sh-route-progress')).toBeTruthy()
    expect(
      container.querySelector('.sh-route-progress')?.getAttribute('aria-busy'),
    ).toBe('true')
    routeLoadEnd()
    await flush()
    expect(container.querySelector('.sh-route-progress')).toBeNull()
  })

  it('stays visible until every overlapping load finishes', async () => {
    const { container } = render(<RouteProgress />)
    routeLoadStart()
    routeLoadStart()
    await flush()
    expect(container.querySelector('.sh-route-progress')).toBeTruthy()
    routeLoadEnd()
    await flush()
    // One load still in flight — bar stays.
    expect(container.querySelector('.sh-route-progress')).toBeTruthy()
    routeLoadEnd()
    await flush()
    expect(container.querySelector('.sh-route-progress')).toBeNull()
  })

  it('never wedges negative on a stray end', async () => {
    const { container } = render(<RouteProgress />)
    routeLoadEnd()
    await flush()
    expect(container.querySelector('.sh-route-progress')).toBeNull()
    routeLoadStart()
    await flush()
    expect(container.querySelector('.sh-route-progress')).toBeTruthy()
    routeLoadEnd()
    await flush()
    expect(container.querySelector('.sh-route-progress')).toBeNull()
  })

  // Regression guard for the #473 hard-freeze: preact-iso calls
  // `routeLoadStart` from inside the Router's *synchronous* suspend handler
  // while a lazy route chunk is still loading. If the signal is mutated
  // synchronously there, Preact re-enters its render mid-diff, re-suspends the
  // route, and re-invokes `routeLoadStart` — an infinite synchronous loop that
  // starves the microtask queue (the dynamic `import()` never resolves) and
  // hard-freezes the tab. The write therefore MUST be deferred: calling
  // `routeLoadStart` must not mutate the bar synchronously.
  it('defers its signal write so it never re-enters render synchronously', async () => {
    const { container } = render(<RouteProgress />)
    routeLoadStart()
    // Synchronously after the call the bar must still be absent — the write is
    // queued, not applied inline. A synchronous mutation here is the freeze.
    expect(container.querySelector('.sh-route-progress')).toBeNull()
    await flush()
    // After the microtask drains, the bar shows.
    expect(container.querySelector('.sh-route-progress')).toBeTruthy()
  })
})
