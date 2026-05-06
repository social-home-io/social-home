/**
 * PublicDiscoveryPage — browse the GFS public-Momentum directory.
 *
 * Picks a paired GFS, fetches its registered-user directory, and
 * lets the caller follow / unfollow each user. Routed at
 * ``/momentum/public/discover``.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { useTitle } from '@/store/pageTitle'
import {
  fetchGfsDirectory,
  follows,
  followUser,
  loadFollows,
  unfollowUser,
} from '@/store/momentPublic'
import type { GfsConnection, MomentPublicDirectoryUser } from '@/types'

const gfses = signal<GfsConnection[]>([])
const selectedGfs = signal<string | null>(null)
const directory = signal<MomentPublicDirectoryUser[]>([])
const loading = signal(true)

async function loadDirectory(gfsId: string): Promise<void> {
  loading.value = true
  try {
    directory.value = await fetchGfsDirectory(gfsId)
  } catch (err) {
    showToast(
      `Directory fetch failed: ${(err as Error)?.message ?? err}`,
      'error',
    )
    directory.value = []
  } finally {
    loading.value = false
  }
}

async function bootstrap(): Promise<void> {
  loading.value = true
  try {
    const conns = await api.get<GfsConnection[]>('/api/gfs/connections')
    gfses.value = conns ?? []
    if (gfses.value.length > 0 && !selectedGfs.value) {
      selectedGfs.value = gfses.value[0].id
    }
    await loadFollows()
    if (selectedGfs.value) await loadDirectory(selectedGfs.value)
  } finally {
    loading.value = false
  }
}

export default function PublicDiscoveryPage() {
  useTitle('Discover Momentum')
  useEffect(() => {
    void bootstrap()
  }, [])

  const isFollowing = (userId: string) =>
    follows.value.some(
      (f) => f.gfs_id === selectedGfs.value && f.followed_user_id === userId,
    )

  const onSelectGfs = (gfsId: string) => {
    selectedGfs.value = gfsId
    void loadDirectory(gfsId)
  }

  const onFollow = async (user: MomentPublicDirectoryUser) => {
    if (!selectedGfs.value) return
    try {
      await followUser(selectedGfs.value, user.user_id)
      showToast(`Following ${user.display_name}`, 'success')
    } catch (err) {
      showToast(
        `Follow failed: ${(err as Error)?.message ?? err}`,
        'error',
      )
    }
  }

  const onUnfollow = async (user: MomentPublicDirectoryUser) => {
    if (!selectedGfs.value) return
    try {
      await unfollowUser(selectedGfs.value, user.user_id)
    } catch (err) {
      showToast(
        `Unfollow failed: ${(err as Error)?.message ?? err}`,
        'error',
      )
    }
  }

  if (gfses.value.length === 0) {
    return (
      <div class="sh-empty-state">
        <h3 style={{ margin: 0 }}>No GFS pairings yet</h3>
        <p>Pair a GFS first to discover other users.</p>
      </div>
    )
  }

  return (
    <div class="sh-momentum-discover">
      <header class="sh-page-header">
        <h2>Discover Momentum</h2>
        {gfses.value.length > 1 && (
          <select
            value={selectedGfs.value ?? ''}
            onChange={(ev) =>
              onSelectGfs((ev.currentTarget as HTMLSelectElement).value)
            }
          >
            {gfses.value.map((g) => (
              <option key={g.id} value={g.id}>
                {g.display_name}
              </option>
            ))}
          </select>
        )}
      </header>

      {loading.value && <p>Loading…</p>}
      {!loading.value && directory.value.length === 0 && (
        <p class="sh-muted">No registered users on this GFS yet.</p>
      )}

      <ul class="sh-momentum-discover-list">
        {directory.value.map((u) => (
          <li key={u.user_id} class="sh-momentum-discover-row">
            <Avatar
              src={u.picture_url}
              name={u.display_name || u.username}
              size={40}
            />
            <div class="sh-momentum-discover-meta">
              <strong>{u.display_name}</strong>
              <span class="sh-muted">@{u.username}</span>
            </div>
            {isFollowing(u.user_id) ? (
              <Button variant="secondary" onClick={() => void onUnfollow(u)}>
                Following
              </Button>
            ) : (
              <Button onClick={() => void onFollow(u)}>Follow</Button>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
