/**
 * Public-Momentum store (§Momentum-public).
 *
 * Caches the current user's GFS registrations + follows so the
 * sidebar / composer can render synchronously without re-fetching.
 * Reads are populated from ``/api/moments/public/*`` on demand;
 * mutations write through the API and update the cache on success.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import type {
  MomentPublicDirectoryUser,
  MomentPublicFollow,
  MomentPublicRegistration,
} from '@/types'

export const registrations = signal<MomentPublicRegistration[]>([])
export const follows = signal<MomentPublicFollow[]>([])

export async function loadRegistrations(): Promise<void> {
  try {
    const rows = (await api.get(
      '/api/moments/public/registrations',
    )) as MomentPublicRegistration[]
    registrations.value = rows ?? []
  } catch {
    /* not authed / transient; leave the cache untouched */
  }
}

export async function loadFollows(): Promise<void> {
  try {
    const rows = (await api.get(
      '/api/moments/public/follows',
    )) as MomentPublicFollow[]
    follows.value = rows ?? []
  } catch {
    /* not authed / transient */
  }
}

export async function registerOnGfs(
  gfsId: string,
  defaultShare = true,
): Promise<void> {
  const row = (await api.post('/api/moments/public/registrations', {
    gfs_id: gfsId,
    default_share: defaultShare,
  })) as MomentPublicRegistration
  registrations.value = [
    ...registrations.value.filter((r) => r.gfs_id !== gfsId),
    row,
  ]
}

export async function deregisterFromGfs(gfsId: string): Promise<void> {
  await api.delete(`/api/moments/public/registrations/${gfsId}`)
  registrations.value = registrations.value.filter((r) => r.gfs_id !== gfsId)
}

export async function setDefaultShare(
  gfsId: string,
  defaultShare: boolean,
): Promise<void> {
  await api.patch(`/api/moments/public/registrations/${gfsId}`, {
    default_share: defaultShare,
  })
  registrations.value = registrations.value.map((r) =>
    r.gfs_id === gfsId ? { ...r, default_share: defaultShare } : r,
  )
}

export async function followUser(
  gfsId: string,
  followedUserId: string,
): Promise<MomentPublicFollow> {
  const row = (await api.post('/api/moments/public/follows', {
    gfs_id: gfsId,
    followed_user_id: followedUserId,
  })) as MomentPublicFollow
  follows.value = [
    ...follows.value.filter(
      (f) =>
        !(f.gfs_id === gfsId && f.followed_user_id === followedUserId),
    ),
    row,
  ]
  return row
}

export async function unfollowUser(
  gfsId: string,
  followedUserId: string,
): Promise<void> {
  await api.delete(
    `/api/moments/public/follows/${gfsId}/${followedUserId}`,
  )
  follows.value = follows.value.filter(
    (f) =>
      !(f.gfs_id === gfsId && f.followed_user_id === followedUserId),
  )
}

export async function fetchGfsDirectory(
  gfsId: string,
): Promise<MomentPublicDirectoryUser[]> {
  const data = (await api.get(`/api/gfs/${gfsId}/moments/users`)) as {
    users?: MomentPublicDirectoryUser[]
  }
  return data.users ?? []
}
