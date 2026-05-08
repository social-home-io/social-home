import { describe, it, expect } from 'vitest'
import { routes } from './router'

describe('router', () => {
  it('defines 40 routes', () => {
    // Routes added / removed across recent passes:
    //   /dms/:id/calls   — per-conversation call history
    //   /calls/:callId   — in-call page
    //   /join            — space invite deep link (§23.62)
    //   /parent          — parent dashboard (§CP)
    //   /feed            — explicit household-feed route (§23 dashboard)
    //   /spaces/:id/settings — admin space settings (§23 customization)
    //   /spaces/browse   — unified space browser (§D3)
    //   /spaces/:id/zones — per-space zones admin (§23.8.7)
    //   /setup           — first-boot wizard (platform/v2)
    //   /friends         — connected-people dashboard under Browse
    //   /highlights, /highlights/new, /highlights/:highlightId — Highlights pillar (§Highlights)
    //   /highlights/archive — month-grid history browser (§Highlights)
    //   /momentum, /momentum/new, /momentum/:id, /momentum/:id/reply,
    //     /momentum/archive — Momentum pillar (§Momentum)
    //   /organize        — replaces standalone /tasks /shopping /stickies
    //                      (3 routes collapsed into 1 hub).
    expect(Object.keys(routes).length).toBe(40)
  })

  it('has feed route at /', () => {
    expect(routes['/']).toBeTruthy()
  })

  it('has all main routes', () => {
    for (const path of ['/spaces', '/dms', '/calendar', '/organize',
      '/notifications', '/pages', '/bazaar',
      '/settings', '/admin', '/connections',
      '/gallery', '/search', '/calls', '/parent', '/momentum',
      '/corner', '/dashboard',
      '/momentum/public/discover', '/momentum/public/sharing']) {
      expect(routes[path]).toBeTruthy()
    }
  })
})
