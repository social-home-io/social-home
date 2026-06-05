import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

describe('NotificationBell', () => {
  it('module exports exist', async () => {
    const mod = await import('./NotificationBell')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })
})

describe('notification polling lifecycle', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('start is idempotent — repeated calls never leak overlapping intervals', async () => {
    const { startNotificationPolling, stopNotificationPolling } =
      await import('./NotificationBell')
    const setSpy = vi.spyOn(globalThis, 'setInterval')
    const clearSpy = vi.spyOn(globalThis, 'clearInterval')

    // Reproduce the old render-body bug: many "starts" back to back.
    // Each start must clear the prior timer so only one interval is
    // ever live (the bug left one orphaned interval per call).
    startNotificationPolling()
    startNotificationPolling()
    startNotificationPolling()

    expect(setSpy).toHaveBeenCalledTimes(3)
    // 2nd + 3rd start each cleared the previous timer → net one live.
    expect(clearSpy.mock.calls.length).toBeGreaterThanOrEqual(2)

    stopNotificationPolling()
    // A redundant stop after the handle is nulled is a no-op.
    const clearsAfterStop = clearSpy.mock.calls.length
    stopNotificationPolling()
    expect(clearSpy.mock.calls.length).toBe(clearsAfterStop)
  })
})
