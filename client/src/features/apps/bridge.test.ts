/**
 * Tests for the host↔iframe postMessage bridge (bridge.ts).
 *
 * Security invariants exercised:
 * - Events from the wrong source are silently dropped (no origin check).
 * - The bearer token NEVER appears in any reply to the iframe.
 * - WS relay forwards only frames whose app_id matches the mounted context.
 */

import { beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'

// ── mock @/ws ──────────────────────────────────────────────────────────────
// Capture every ws.on registration so tests can drive synthetic frames.
type WsHandler = (evt: { data: Record<string, unknown> }) => void
const wsHandlers: Record<string, WsHandler> = {}
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const wsOffFns: Record<string, MockInstance<any, any>> = {}

vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, handler: WsHandler) => {
      wsHandlers[type] = handler
      const off = vi.fn(() => { delete wsHandlers[type] })
      wsOffFns[type] = off
      return off
    },
  },
}))

// ── mock @/api ─────────────────────────────────────────────────────────────
const mockApiGet = vi.fn()
const mockApiPut = vi.fn()
const mockApiDelete = vi.fn()

vi.mock('@/api', () => {
  class ApiError extends Error {
    constructor(
      public readonly status: number,
      public readonly path: string,
    ) {
      super(`API ${status}: ${path}`)
      this.name = 'ApiError'
    }
  }

  return {
    ApiError,
    api: {
      get: (...args: unknown[]) => mockApiGet(...args),
      put: (...args: unknown[]) => mockApiPut(...args),
      delete: (...args: unknown[]) => mockApiDelete(...args),
    },
  }
})

import { mountBridge } from './bridge'
// Import the mocked ApiError so tests can construct it.
import { ApiError } from '@/api'

// ── helpers ────────────────────────────────────────────────────────────────

const APP_ID = 'app-chess'
const USER_ID = 'user-42'

function makeFakeIframe() {
  return {
    contentWindow: { postMessage: vi.fn() },
  } as unknown as HTMLIFrameElement
}

/** Capture the ``message`` handler registered by mountBridge. */
function captureMessageHandler(): (event: Partial<MessageEvent>) => Promise<void> {
  // window.addEventListener is spied on in beforeEach; read mock.calls
  // to find the 'message' entry that mountBridge registered.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const spy = window.addEventListener as unknown as { mock: { calls: any[][] } }
  const calls = spy.mock?.calls ?? []
  const last = [...calls].reverse().find(([type]) => type === 'message')
  const raw: (event: Partial<MessageEvent>) => void =
    last ? (last[1] as (event: Partial<MessageEvent>) => void) : () => {}
  // Wrap the synchronous listener so the test can await the async
  // work that ``onMessage`` fires-and-forgets via ``handleRpc``.
  return async (event: Partial<MessageEvent>) => {
    raw(event)
    // Flush all pending microtasks so handleRpc's awaits resolve.
    await new Promise<void>(resolve => setTimeout(resolve, 0))
  }
}

// ── test suite ─────────────────────────────────────────────────────────────

describe('mountBridge', () => {
  let iframe: HTMLIFrameElement
  let cleanup: () => void
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let addEventSpy: MockInstance<any, any>
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let removeEventSpy: MockInstance<any, any>

  beforeEach(() => {
    mockApiGet.mockReset()
    mockApiPut.mockReset()
    mockApiDelete.mockReset()
    Object.keys(wsHandlers).forEach(k => delete wsHandlers[k])
    Object.keys(wsOffFns).forEach(k => delete wsOffFns[k])

    iframe = makeFakeIframe()

    addEventSpy = vi.spyOn(window, 'addEventListener')
    removeEventSpy = vi.spyOn(window, 'removeEventListener')

    cleanup = mountBridge(iframe, { appId: APP_ID, selfUserId: USER_ID })
  })

  // ── wrong source ─────────────────────────────────────────────────────────

  it('ignores events whose source is not iframe.contentWindow', async () => {
    const otherWindow = { postMessage: vi.fn() } as unknown as Window
    const handler = captureMessageHandler()

    await handler({ source: otherWindow, data: { id: 1, method: 'store.list' } })

    expect(mockApiGet).not.toHaveBeenCalled()
    expect(iframe.contentWindow!.postMessage).not.toHaveBeenCalled()
  })

  // ── store.set ────────────────────────────────────────────────────────────

  it('store.set: calls api.put with correct path and body; replies ok:true', async () => {
    mockApiPut.mockResolvedValueOnce(undefined)
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 7, method: 'store.set', params: { key: 'score', value: 99 } },
    })

    expect(mockApiPut).toHaveBeenCalledWith(
      `/api/apps/${APP_ID}/store/score`,
      { value: 99 },
    )
    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 7, ok: true }),
      '*',
    )
  })

  it('store.set: encodes special characters in the key', async () => {
    mockApiPut.mockResolvedValueOnce(undefined)
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 8, method: 'store.set', params: { key: 'a/b c', value: 1 } },
    })

    expect(mockApiPut).toHaveBeenCalledWith(
      `/api/apps/${APP_ID}/store/${encodeURIComponent('a/b c')}`,
      { value: 1 },
    )
  })

  // ── store.get ────────────────────────────────────────────────────────────

  it('store.get: returns the value field from the API response', async () => {
    mockApiGet.mockResolvedValueOnce({ key: 'score', value: 42 })
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 10, method: 'store.get', params: { key: 'score' } },
    })

    expect(mockApiGet).toHaveBeenCalledWith(
      `/api/apps/${APP_ID}/store/${encodeURIComponent('score')}`,
    )
    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 10, ok: true, result: 42 }),
      '*',
    )
  })

  it('store.get: returns null when the key is missing (404)', async () => {
    mockApiGet.mockRejectedValueOnce(new ApiError(404, `/api/apps/${APP_ID}/store/missing`))
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 11, method: 'store.get', params: { key: 'missing' } },
    })

    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 11, ok: true, result: null }),
      '*',
    )
  })

  it('store.get: replies ok:false for non-404 API errors', async () => {
    mockApiGet.mockRejectedValueOnce(new ApiError(500, `/api/apps/${APP_ID}/store/x`))
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 12, method: 'store.get', params: { key: 'x' } },
    })

    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 12, ok: false }),
      '*',
    )
  })

  // ── app.context ──────────────────────────────────────────────────────────

  it('app.context: returns appId and selfUserId — no token field', async () => {
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 20, method: 'app.context' },
    })

    const call = (iframe.contentWindow!.postMessage as ReturnType<typeof vi.fn>).mock.calls[0]
    const reply = call[0] as { id: number; ok: boolean; result: Record<string, unknown> }

    expect(reply.ok).toBe(true)
    expect(reply.result.appId).toBe(APP_ID)
    expect(reply.result.selfUserId).toBe(USER_ID)
    // Security: the bearer token must never be forwarded to the app iframe.
    expect(reply.result).not.toHaveProperty('token')
  })

  // ── unknown method ────────────────────────────────────────────────────────

  it('unknown method: replies ok:false', async () => {
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 30, method: 'does.not.exist' },
    })

    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 30, ok: false }),
      '*',
    )
    const call = (iframe.contentWindow!.postMessage as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(call[0].error.code).toBe('unknown_method')
  })

  // ── app.send ──────────────────────────────────────────────────────────────

  it('app.send: replies ok:false with code "unavailable"', async () => {
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 40, method: 'app.send', params: { target: 'peer', data: {} } },
    })

    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 40, ok: false }),
      '*',
    )
    const call = (iframe.contentWindow!.postMessage as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(call[0].error.code).toBe('unavailable')
  })

  // ── WS relay ──────────────────────────────────────────────────────────────

  it('WS relay: forwards app.message frames matching the appId into the iframe', () => {
    expect(wsHandlers['app.message']).toBeDefined()

    wsHandlers['app.message']({
      data: { app_id: APP_ID, payload: { move: 'e4' } },
    })

    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      { type: 'app:event', payload: { move: 'e4' } },
      '*',
    )
  })

  it('WS relay: does NOT forward frames for a different app_id', () => {
    wsHandlers['app.message']({
      data: { app_id: 'other-app', payload: { move: 'd5' } },
    })

    expect(iframe.contentWindow!.postMessage).not.toHaveBeenCalled()
  })

  // ── cleanup ──────────────────────────────────────────────────────────────

  it('cleanup: removes the message listener from window', () => {
    // Find the handler that was registered in beforeEach
    const addCalls = addEventSpy.mock.calls
    const messageHandler = addCalls.find(([type]) => type === 'message')?.[1]
    expect(messageHandler).toBeDefined()

    cleanup()

    expect(removeEventSpy).toHaveBeenCalledWith('message', messageHandler)
  })

  it('cleanup: calls the ws unsubscribe function', () => {
    const offFn = wsOffFns['app.message']
    expect(offFn).toBeDefined()

    cleanup()

    expect(offFn).toHaveBeenCalled()
  })

  // ── store.list ────────────────────────────────────────────────────────────

  it('store.list: calls api.get on /store and returns items', async () => {
    mockApiGet.mockResolvedValueOnce({ items: ['a', 'b'] })
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 50, method: 'store.list' },
    })

    expect(mockApiGet).toHaveBeenCalledWith(`/api/apps/${APP_ID}/store`)
    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 50, ok: true, result: ['a', 'b'] }),
      '*',
    )
  })

  // ── store.delete ──────────────────────────────────────────────────────────

  it('store.delete: calls api.delete with correct path and replies ok:true', async () => {
    mockApiDelete.mockResolvedValueOnce(undefined)
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 60, method: 'store.delete', params: { key: 'score' } },
    })

    expect(mockApiDelete).toHaveBeenCalledWith(
      `/api/apps/${APP_ID}/store/${encodeURIComponent('score')}`,
    )
    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 60, ok: true }),
      '*',
    )
  })
})
