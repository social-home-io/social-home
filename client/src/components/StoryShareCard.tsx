/**
 * StoryShareCard — feed-card body for ``post.type === 'story_share'``.
 *
 * Lazy-fetches the linked story via ``GET /api/stories/{id}`` (the
 * server already gates by audience, so a 404 here means "expired" or
 * "not visible to me"). Renders a thumbnail of the first frame plus the
 * sharer's optional note + a "Tap to watch" affordance that routes to
 * :class:`StoryViewerPage`.
 *
 * Three render states: active (thumb + watch button), expired
 * ("Story has ended"), no link (``story_id === null`` because retention
 * already nulled the FK on the household table).
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import type { Story, StoryFrame } from '@/types'

interface StoryDetailResponse {
  story: Story
  frames: StoryFrame[]
}

export interface StoryShareCardProps {
  /** ``post.linked_story_id`` — null when retention purged the source. */
  storyId: string | null
  /** ``post.content`` — the sharer's optional caption. */
  note?: string | null
}

const cache = signal<Record<string, StoryDetailResponse | null>>({})
const loading = signal<Record<string, boolean>>({})


export function StoryShareCard({ storyId, note }: StoryShareCardProps) {
  const loc = useLocation()

  useEffect(() => {
    if (!storyId) return
    if (storyId in cache.value) return
    if (loading.value[storyId]) return
    loading.value = { ...loading.value, [storyId]: true }
    api.get(`/api/stories/${storyId}`)
      .then((d: StoryDetailResponse) => {
        cache.value = { ...cache.value, [storyId]: d }
      })
      .catch(() => {
        // 404 / 403 → cache null so we render the "ended" placeholder
        // and don't retry on every re-render.
        cache.value = { ...cache.value, [storyId]: null }
      })
      .finally(() => {
        const next = { ...loading.value }
        delete next[storyId]
        loading.value = next
      })
  }, [storyId])

  if (!storyId) {
    return (
      <div class="sh-story-share-card sh-story-share-card--ended">
        <span class="sh-story-share-card-thumb sh-story-share-card-thumb--ended">
          ⏳
        </span>
        <div class="sh-story-share-card-body">
          <strong>Story has ended</strong>
          {note && <p class="sh-muted">{note}</p>}
        </div>
      </div>
    )
  }

  const detail = cache.value[storyId]
  if (loading.value[storyId] || detail === undefined) {
    return (
      <div class="sh-story-share-card sh-story-share-card--loading">
        <span class="sh-story-share-card-thumb sh-story-share-card-thumb--loading" />
        <div class="sh-story-share-card-body">
          <strong>Loading story…</strong>
        </div>
      </div>
    )
  }
  if (detail === null) {
    return (
      <div class="sh-story-share-card sh-story-share-card--ended">
        <span class="sh-story-share-card-thumb sh-story-share-card-thumb--ended">
          ⏳
        </span>
        <div class="sh-story-share-card-body">
          <strong>Story has ended</strong>
          {note && <p class="sh-muted">{note}</p>}
        </div>
      </div>
    )
  }

  const first = detail.frames[0]
  return (
    <button
      type="button"
      class="sh-story-share-card"
      onClick={() => loc.route(`/stories/${detail.story.id}`)}
      aria-label={`Watch ${detail.story.author_user_id}'s story`}
    >
      {first && first.frame_type === 'image' ? (
        <img
          src={first.media_url}
          alt=""
          loading="lazy"
          class="sh-story-share-card-thumb"
        />
      ) : (
        <span class="sh-story-share-card-thumb sh-story-share-card-thumb--video">
          🎬
        </span>
      )}
      <div class="sh-story-share-card-body">
        <strong>{detail.story.author_user_id}</strong>
        <span class="sh-muted">
          {detail.story.story_date} · {detail.frames.length} frame{detail.frames.length === 1 ? '' : 's'}
        </span>
        {note && <p>{note}</p>}
        <span class="sh-story-share-card-cta">Tap to watch →</span>
      </div>
    </button>
  )
}
