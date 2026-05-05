import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  blockedUserIds,
  blockedUsers,
  blockUser,
  isBlocked,
  loadBlocks,
  unblockUser,
  _resetBlocksForTest,
} from './blocks'

const fetchMock = vi.fn()

beforeEach(() => {
  _resetBlocksForTest()
  fetchMock.mockReset()
  globalThis.fetch = fetchMock as unknown as typeof fetch
  // No auth token in localStorage — the api client just sends an
  // Authorization header when present, the server treats it as 401
  // otherwise. We're stubbing fetch, so the header doesn't matter.
})

afterEach(() => {
  _resetBlocksForTest()
})

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { 'content-type': 'application/json' },
  })
}

describe('blocks store', () => {
  it('loadBlocks populates the cache and id set', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ blocks: [
        { user_id: 'uid-bob', blocked_at: '2026-05-04T00:00:00Z' },
        { user_id: 'uid-carol', blocked_at: '2026-05-03T00:00:00Z' },
      ] }),
    )
    const rows = await loadBlocks()
    expect(rows.map(r => r.user_id)).toEqual(['uid-bob', 'uid-carol'])
    expect(isBlocked('uid-bob')).toBe(true)
    expect(isBlocked('uid-nobody')).toBe(false)
  })

  it('blockUser optimistically inserts into the cache', async () => {
    // initial load
    fetchMock.mockResolvedValueOnce(jsonResponse({ blocks: [] }))
    await loadBlocks()
    // POST /api/blocks
    fetchMock.mockResolvedValueOnce(jsonResponse({ user_id: 'uid-bob' }, { status: 201 }))
    await blockUser('uid-bob')
    expect(blockedUserIds.value.has('uid-bob')).toBe(true)
    expect(blockedUsers.value[0]).toMatchObject({ user_id: 'uid-bob' })
  })

  it('unblockUser removes from the cache', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ blocks: [{ user_id: 'uid-bob', blocked_at: 'x' }] }),
    )
    await loadBlocks()
    fetchMock.mockResolvedValueOnce(new Response('', { status: 200 }))
    await unblockUser('uid-bob')
    expect(blockedUserIds.value.has('uid-bob')).toBe(false)
    expect(blockedUsers.value).toEqual([])
  })

  it('isBlocked returns false for null/undefined', () => {
    expect(isBlocked(null)).toBe(false)
    expect(isBlocked(undefined)).toBe(false)
  })

  it('loadBlocks is memoised; force re-fetches', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ blocks: [] }))
    await loadBlocks()
    await loadBlocks()  // second call hits the cache, no fetch
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await loadBlocks(true)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})
