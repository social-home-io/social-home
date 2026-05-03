/**
 * StoriesPage — inbox for the personal Stories pillar (§Stories).
 *
 * Renders a horizontal "rings" row at the top (avatars circled with a
 * terracotta glow when the viewer has unseen frames) plus a per-author
 * grouped list below. Tapping a ring or row entry routes to
 * :class:`StoryViewerPage` with that story id.
 *
 * Authors get a "+ New" tile leading to :class:`StoryComposerPage`.
 *
 * Audience filtering happens server-side; this page just renders the
 * inbox the API returns.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { StoriesRingSkeleton } from '@/components/Skeleton'
import { Avatar } from '@/components/Avatar'
import { showToast } from '@/components/Toast'
import { currentUser } from '@/store/auth'
import type { StoryInboxItem } from '@/types'

const inbox = signal<StoryInboxItem[]>([])
const loading = signal<boolean>(true)


function humaniseDate(iso: string): string {
  // Stories list groups by ``story_date`` (YYYY-MM-DD UTC). For a
  // friendlier inbox we surface "Today" / "Yesterday" / weekday for
  // the recent past, and a fully spelled-out date otherwise.
  const today = new Date()
  const todayStr = today.toISOString().slice(0, 10)
  if (iso === todayStr) return 'Today'
  const yest = new Date(today)
  yest.setUTCDate(yest.getUTCDate() - 1)
  if (iso === yest.toISOString().slice(0, 10)) return 'Yesterday'
  const dt = new Date(iso + 'T00:00:00Z')
  if (Number.isNaN(dt.getTime())) return iso
  return dt.toLocaleDateString(undefined, {
    weekday: 'long',
    month: 'short',
    day: 'numeric',
  })
}


export default function StoriesPage() {
  const loc = useLocation()
  const me = currentUser.value?.user_id

  useEffect(() => {
    loading.value = true
    api.get('/api/stories')
      .then((rows: StoryInboxItem[]) => {
        inbox.value = rows ?? []
        loading.value = false
      })
      .catch((err: unknown) => {
        showToast(`Failed to load stories: ${(err as Error)?.message ?? err}`,
          'error')
        loading.value = false
      })
  }, [])

  if (loading.value) return <StoriesRingSkeleton />

  const items = inbox.value

  // Build a "rings" row: my own + everyone else's, grouped by author.
  const myItems = items.filter(i => i.story.author_user_id === me)
  const peerItems = items.filter(i => i.story.author_user_id !== me)

  const ring = (item: StoryInboxItem) => {
    const cls = item.unseen_count > 0
      ? 'sh-story-ring sh-story-ring--unseen'
      : 'sh-story-ring'
    const onClick = () => loc.route(`/stories/${item.story.id}`)
    return (
      <button
        key={item.story.id}
        type="button"
        class={cls}
        onClick={onClick}
        aria-label={`Open ${item.story.author_user_id}'s story`}
      >
        <span class="sh-story-ring-avatar">
          <Avatar name={item.story.author_user_id} size={56} />
        </span>
        <span class="sh-story-ring-label">
          {item.story.author_user_id === me ? 'Your story' : item.story.author_user_id}
        </span>
      </button>
    )
  }

  return (
    <div class="sh-stories-page">
      <header class="sh-stories-header">
        <h2>Stories</h2>
        <a href="/settings#stories" class="sh-link sh-stories-settings-link">
          Settings
        </a>
      </header>

      <section class="sh-story-rings" aria-label="Recent stories">
        <button
          type="button"
          class="sh-story-ring sh-story-ring--new"
          onClick={() => loc.route('/stories/new')}
          aria-label="Post a new story"
        >
          <span class="sh-story-ring-avatar">
            <span class="sh-story-ring-plus" aria-hidden="true">+</span>
          </span>
          <span class="sh-story-ring-label">New</span>
        </button>
        {myItems.map(ring)}
        {peerItems.map(ring)}
      </section>

      {items.length === 0 && (
        <p class="sh-muted sh-stories-empty">
          No stories yet. Tap <strong>+ New</strong> to share a moment with your
          household and connected peers.
        </p>
      )}

      {items.length > 0 && (
        <section class="sh-story-list" aria-label="All stories">
          {items.map(item => {
            const first = item.frames[0]
            return (
              <a
                key={item.story.id}
                href={`/stories/${item.story.id}`}
                class="sh-story-row"
              >
                {first && first.frame_type === 'image' && (
                  <img
                    src={first.media_url}
                    alt=""
                    loading="lazy"
                    class="sh-story-row-thumb"
                  />
                )}
                {first && first.frame_type === 'video' && (
                  <span class="sh-story-row-thumb sh-story-row-thumb--video">
                    🎬
                  </span>
                )}
                {!first && (
                  <span class="sh-story-row-thumb sh-story-row-thumb--empty" />
                )}
                <span class="sh-story-row-meta">
                  <strong>
                    {item.story.author_user_id === me
                      ? 'You'
                      : item.story.author_user_id}
                  </strong>
                  <span class="sh-muted">
                    {humaniseDate(item.story.story_date)} ·{' '}
                    {item.frames.length} frame{item.frames.length === 1 ? '' : 's'}
                  </span>
                </span>
                {item.unseen_count > 0 && (
                  <span class="sh-story-row-unseen">{item.unseen_count}</span>
                )}
              </a>
            )
          })}
        </section>
      )}
    </div>
  )
}
