import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  followedUserIds,
  followedUsers,
  followUser,
  isFollowing,
  loadFollows,
  unfollowUser,
  _resetFollowsForTest,
} from './follows'

const fetchMock = vi.fn()

beforeEach(() => {
  _resetFollowsForTest()
  fetchMock.mockReset()
  globalThis.fetch = fetchMock as unknown as typeof fetch
})

afterEach(() => {
  _resetFollowsForTest()
})

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { 'content-type': 'application/json' },
  })
}

describe('follows store', () => {
  it('loadFollows populates the cache and id set', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ follows: [
        { user_id: 'uid-bob', created_at: '2026-05-04T00:00:00Z' },
        { user_id: 'uid-carol', created_at: '2026-05-03T00:00:00Z' },
      ] }),
    )
    const rows = await loadFollows()
    expect(rows.map(r => r.user_id)).toEqual(['uid-bob', 'uid-carol'])
    expect(isFollowing('uid-bob')).toBe(true)
    expect(isFollowing('uid-nobody')).toBe(false)
  })

  it('followUser optimistically inserts into the cache', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ follows: [] }))
    await loadFollows()
    fetchMock.mockResolvedValueOnce(jsonResponse({ user_id: 'uid-bob' }, { status: 201 }))
    await followUser('uid-bob')
    expect(followedUserIds.value.has('uid-bob')).toBe(true)
    expect(followedUsers.value[0]).toMatchObject({ user_id: 'uid-bob' })
  })

  it('unfollowUser removes from the cache', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ follows: [{ user_id: 'uid-bob', created_at: 'x' }] }),
    )
    await loadFollows()
    fetchMock.mockResolvedValueOnce(new Response('', { status: 200 }))
    await unfollowUser('uid-bob')
    expect(followedUserIds.value.has('uid-bob')).toBe(false)
    expect(followedUsers.value).toEqual([])
  })

  it('isFollowing returns false for null/undefined', () => {
    expect(isFollowing(null)).toBe(false)
    expect(isFollowing(undefined)).toBe(false)
  })

  it('loadFollows is memoised; force re-fetches', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ follows: [] }))
    await loadFollows()
    await loadFollows()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await loadFollows(true)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})
