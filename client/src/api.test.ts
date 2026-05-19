import { describe, it, expect, vi, beforeEach } from 'vitest'
import { api, ApiError } from './api'

describe('api — surface', () => {
  it('exports an ApiClient instance', () => {
    expect(api).toBeTruthy()
    expect(typeof api.get).toBe('function')
    expect(typeof api.post).toBe('function')
    expect(typeof api.patch).toBe('function')
    expect(typeof api.delete).toBe('function')
  })
})

describe('ApiError — friendly-detail unwrap', () => {
  /** Replace ``global.fetch`` with a stub that returns the supplied
   *  ``status`` and parsed JSON body. Calling code only awaits
   *  ``res.json()`` once per request, so a single fixed body is enough. */
  function stubFetch(status: number, body: unknown) {
    const res = {
      ok: status >= 200 && status < 300,
      status,
      json: vi.fn().mockResolvedValue(body),
    } as unknown as Response
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(res))
  }

  beforeEach(() => {
    vi.unstubAllGlobals()
  })

  it('parses {error: {code, detail}} into ApiError.code + .detail and uses detail as message', async () => {
    stubFetch(422, {
      error: {
        code: 'UNPROCESSABLE',
        detail: 'Home Assistant has no picture for this user.',
      },
    })
    try {
      await api.post('/api/me/picture/refresh-from-ha', {})
      expect.fail('should have thrown')
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError)
      const err = e as ApiError
      expect(err.status).toBe(422)
      expect(err.code).toBe('UNPROCESSABLE')
      expect(err.detail).toBe('Home Assistant has no picture for this user.')
      // ``Error.message`` is the detail — that's what
      // ``showToast(err.message)`` will display.
      expect(err.message).toBe('Home Assistant has no picture for this user.')
    }
  })

  it('falls back to "API <status>: <path>" when the body is not the canonical shape', async () => {
    stubFetch(502, '<html>Bad Gateway</html>')
    try {
      await api.get('/api/whatever')
      expect.fail('should have thrown')
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError)
      const err = e as ApiError
      expect(err.status).toBe(502)
      expect(err.code).toBeNull()
      expect(err.detail).toBeNull()
      expect(err.message).toBe('API 502: /api/whatever')
    }
  })

  it('falls back to "API <status>: <path>" when the body is empty / unparseable', async () => {
    const res = {
      ok: false,
      status: 500,
      json: vi.fn().mockRejectedValue(new Error('not json')),
    } as unknown as Response
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(res))
    try {
      await api.delete('/api/foo')
      expect.fail('should have thrown')
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError)
      const err = e as ApiError
      expect(err.message).toBe('API 500: /api/foo')
    }
  })

  it('ignores garbage in the error body (non-string code / detail) and still sets status', async () => {
    stubFetch(404, { error: { code: 42, detail: ['nope'] } })
    try {
      await api.get('/api/foo')
      expect.fail('should have thrown')
    } catch (e) {
      const err = e as ApiError
      expect(err.code).toBeNull()
      expect(err.detail).toBeNull()
      expect(err.status).toBe(404)
      // No friendly detail to use — fall back to the historic shape.
      expect(err.message).toBe('API 404: /api/foo')
    }
  })
})
