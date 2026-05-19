/**
 * Tests for the connections store's WS handler wiring.
 *
 * ``wireConnectionsWs()`` is the single source of truth for
 * ``peer.home_changed`` and ``local.home_changed`` frame handling.
 * We mock the ``ws`` module so we can drive synthetic frames at the
 * registered callbacks and assert that the signals mutate correctly.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

const handlers: Record<string, (e: { data: Record<string, unknown> }) => void> = {}

vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: (e: { data: Record<string, unknown> }) => void) => {
      handlers[type] = h
      return () => { delete handlers[type] }
    },
  },
}))

import { connections, selfLat, selfLon, wireConnectionsWs } from './connections'

describe('wireConnectionsWs', () => {
  beforeEach(() => {
    connections.value = []
    selfLat.value = null
    selfLon.value = null
    Object.keys(handlers).forEach(k => delete handlers[k])
    wireConnectionsWs()
  })

  it('local.home_changed updates selfLat and selfLon signals', () => {
    handlers['local.home_changed']({ data: { latitude: 52.52, longitude: 13.405 } })
    expect(selfLat.value).toBe(52.52)
    expect(selfLon.value).toBe(13.405)
  })

  it('local.home_changed is a no-op when latitude is missing', () => {
    handlers['local.home_changed']({ data: { longitude: 13.405 } })
    expect(selfLat.value).toBeNull()
    expect(selfLon.value).toBeNull()
  })

  it('local.home_changed is a no-op when longitude is missing', () => {
    handlers['local.home_changed']({ data: { latitude: 52.52 } })
    expect(selfLat.value).toBeNull()
    expect(selfLon.value).toBeNull()
  })

  it('peer.home_changed updates the matching connection coords', () => {
    connections.value = [
      { instance_id: 'peer-1', display_name: 'Bob', reachable: true, home_lat: null, home_lon: null },
      { instance_id: 'peer-2', display_name: 'Carol', reachable: true, home_lat: 50.0, home_lon: 8.0 },
    ]
    handlers['peer.home_changed']({
      data: { instance_id: 'peer-1', latitude: 53.55, longitude: 9.99 },
    })
    const updated = connections.value.find(c => c.instance_id === 'peer-1')
    expect(updated?.home_lat).toBe(53.55)
    expect(updated?.home_lon).toBe(9.99)
    // peer-2 must remain unchanged
    expect(connections.value.find(c => c.instance_id === 'peer-2')?.home_lon).toBe(8.0)
  })

  it('peer.home_changed for unknown instance_id is a no-op', () => {
    connections.value = [
      { instance_id: 'peer-1', reachable: true, home_lat: 1.0, home_lon: 2.0 },
    ]
    handlers['peer.home_changed']({
      data: { instance_id: 'peer-unknown', latitude: 99, longitude: 99 },
    })
    expect(connections.value).toHaveLength(1)
    expect(connections.value[0].home_lat).toBe(1.0)
    expect(connections.value[0].home_lon).toBe(2.0)
  })

  it('peer.home_changed is a no-op when instance_id is missing', () => {
    connections.value = [
      { instance_id: 'peer-1', reachable: true, home_lat: 1.0, home_lon: 2.0 },
    ]
    handlers['peer.home_changed']({
      data: { latitude: 99, longitude: 99 },
    })
    expect(connections.value[0].home_lat).toBe(1.0)
  })

  it('peer.home_changed is a no-op when latitude is null', () => {
    connections.value = [
      { instance_id: 'peer-1', reachable: true, home_lat: 1.0, home_lon: 2.0 },
    ]
    handlers['peer.home_changed']({
      data: { instance_id: 'peer-1', longitude: 9.99 },
    })
    expect(connections.value[0].home_lat).toBe(1.0)
    expect(connections.value[0].home_lon).toBe(2.0)
  })
})
