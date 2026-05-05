/**
 * StoryViewerPage — full-screen tap-through viewer for one story (§Stories).
 *
 * Renders frames sequentially with a top progress bar (one segment per
 * frame). Tap left/right to navigate, hold to pause, swipe up (or click
 * the reactions chip) to drop a quick reaction. When viewing someone
 * else's frame a "Reply" chip surfaces below the frame; tapping it
 * routes to the DM thread with the frame's snapshot pre-loaded.
 */
import { useEffect, useRef } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useRoute, useLocation } from 'preact-iso'
import { api } from '@/api'
import { Spinner } from '@/components/Spinner'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { currentUser } from '@/store/auth'
import type { Story, StoryFrame } from '@/types'
import { confirmDialog } from '@/components/confirm'
import { openUserActions } from '@/components/UserActionsMenu'
import { ws } from '@/ws'

interface StoryDetail {
  story: Story
  frames: StoryFrame[]
  views?: Record<string, { viewer_user_id: string; viewed_at: string }[]>
  reactions?: Record<string, { reactor_user_id: string; emoji: string }[]>
}

const QUICK_REACTIONS = ['❤️', '🔥', '😂', '😮', '😢', '👏'] as const

const FRAME_DURATION_MS = 6000  // image default; video uses its own length

const detail = signal<StoryDetail | null>(null)
const loading = signal<boolean>(true)
const currentIndex = signal<number>(0)
const paused = signal<boolean>(false)
const reactionsOpen = signal<boolean>(false)


export default function StoryViewerPage() {
  const { params } = useRoute()
  const loc = useLocation()
  const storyId = params.storyId
  const myId = currentUser.value?.user_id

  // ── Load the story detail on mount / id change ─────────────────────
  useEffect(() => {
    loading.value = true
    detail.value = null
    currentIndex.value = 0
    paused.value = false
    reactionsOpen.value = false
    const refetch = (initial: boolean) =>
      api.get(`/api/stories/${storyId}`)
        .then((d: StoryDetail) => {
          detail.value = d
          if (initial) loading.value = false
        })
        .catch((err: unknown) => {
          if (initial) {
            showToast(
              `Couldn't load story: ${(err as Error)?.message ?? err}`,
              'error',
            )
            loc.route('/stories')
          }
        })
    void refetch(true)
    // Live counters for authors viewing their own story page —
    // ``story.frame_viewed`` / ``story.frame_reaction_changed`` arrive
    // when a peer's viewer marks a frame seen or reacts. We mutate the
    // local ``detail`` signal in place (cheap O(1) updates per event)
    // instead of refetching the whole story — both events carry every
    // field the UI needs.
    const matches = (data: { story_id?: string }) => data.story_id === storyId
    const dispose = [
      ws.on('story.frame_viewed', (e) => {
        const data = e.data as {
          story_id?: string; frame_id?: string; viewer_user_id?: string
        }
        if (!matches(data) || !detail.value || !data.frame_id) return
        const frameId = data.frame_id
        const viewer = data.viewer_user_id ?? ''
        const views = { ...(detail.value.views ?? {}) }
        const list = views[frameId] ?? []
        // Idempotent — a viewer marking the same frame twice is a no-op.
        if (list.some(v => v.viewer_user_id === viewer)) return
        views[frameId] = [
          ...list,
          { viewer_user_id: viewer, viewed_at: new Date().toISOString() },
        ]
        detail.value = { ...detail.value, views }
      }),
      ws.on('story.frame_reaction_changed', (e) => {
        const data = e.data as {
          story_id?: string; frame_id?: string;
          reactor_user_id?: string; emoji?: string | null
        }
        if (!matches(data) || !detail.value || !data.frame_id) return
        const frameId = data.frame_id
        const reactor = data.reactor_user_id ?? ''
        const reactions = { ...(detail.value.reactions ?? {}) }
        const list = (reactions[frameId] ?? []).filter(
          r => r.reactor_user_id !== reactor,
        )
        if (data.emoji) {
          list.push({ reactor_user_id: reactor, emoji: data.emoji })
        }
        reactions[frameId] = list
        detail.value = { ...detail.value, reactions }
      }),
    ]
    return () => { dispose.forEach(d => d()) }
  }, [storyId])

  // Auto-progress: stamp a per-frame timer that ticks the index.
  // Stored as a ref so pause/resume can clear/recreate without losing
  // the "remaining" budget. This is intentionally simple — perfect
  // pause-resume to the millisecond is overkill for v1.
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const frameStartRef = useRef<number>(Date.now())

  useEffect(() => {
    if (!detail.value || loading.value) return
    const frame = detail.value.frames[currentIndex.value]
    if (!frame) return
    frameStartRef.current = Date.now()
    if (timerRef.current) clearTimeout(timerRef.current)
    if (paused.value) return
    const dur = frame.duration_ms && frame.duration_ms > 0
      ? frame.duration_ms
      : FRAME_DURATION_MS
    timerRef.current = setTimeout(() => {
      advance()
    }, dur)
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current)
    }
  }, [currentIndex.value, detail.value?.story.id, paused.value, loading.value])

  // Mark each frame viewed exactly once on entry.
  useEffect(() => {
    if (!detail.value) return
    const frame = detail.value.frames[currentIndex.value]
    if (!frame) return
    if (detail.value.story.author_user_id === myId) return  // authors don't mark
    api.post(`/api/stories/frames/${frame.id}/view`, {}).catch(() => {})
  }, [currentIndex.value, detail.value?.story.id])

  const advance = () => {
    if (!detail.value) return
    if (currentIndex.value < detail.value.frames.length - 1) {
      currentIndex.value += 1
    } else {
      loc.route('/stories')  // end of story → back to inbox
    }
  }
  const goBack = () => {
    if (currentIndex.value > 0) currentIndex.value -= 1
  }

  if (loading.value) return <Spinner />
  if (!detail.value) return null

  const story = detail.value.story
  const frames = detail.value.frames
  const frame = frames[currentIndex.value]
  if (!frame) {
    return (
      <div class="sh-story-viewer">
        <p class="sh-muted">This story has no frames.</p>
        <Button onClick={() => loc.route('/stories')}>Back</Button>
      </div>
    )
  }
  const isAuthor = story.author_user_id === myId

  const onTapLeft = (e: Event) => { e.preventDefault(); goBack() }
  const onTapRight = (e: Event) => { e.preventDefault(); advance() }

  const react = async (emoji: string) => {
    try {
      await api.put(`/api/stories/frames/${frame.id}/reaction`, { emoji })
      reactionsOpen.value = false
      showToast('Reaction sent', 'success')
    } catch (err: unknown) {
      showToast(`Reaction failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  const dmReply = () => {
    // The snapshot is built server-side; the SPA jumps to the DM list
    // pre-filled with the author so the user can pick / start a thread.
    loc.route(`/dms?story_frame_id=${encodeURIComponent(frame.id)}`)
  }

  const deleteFrame = async () => {
    if (!await confirmDialog('Delete this frame?', { destructive: true })) return
    try {
      await api.delete(`/api/stories/frames/${frame.id}`)
      showToast('Frame removed', 'info')
      // Drop the frame from local state and re-index.
      const next = frames.filter(f => f.id !== frame.id)
      detail.value = { ...detail.value!, frames: next }
      if (currentIndex.value >= next.length) {
        currentIndex.value = Math.max(0, next.length - 1)
      }
      if (next.length === 0) loc.route('/stories')
    } catch (err: unknown) {
      showToast(`Delete failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  // Build a per-frame views/reactions footer the author sees.
  const myViews = detail.value.views?.[frame.id] ?? []
  const myReactions = detail.value.reactions?.[frame.id] ?? []

  return (
    <div
      class="sh-story-viewer"
      onPointerDown={() => { paused.value = true }}
      onPointerUp={() => { paused.value = false }}
      onPointerLeave={() => { paused.value = false }}
    >
      {/* Progress bar — one segment per frame. */}
      <div class="sh-story-progress" aria-hidden="true">
        {frames.map((_, i) => (
          <span
            key={i}
            class={
              i < currentIndex.value
                ? 'sh-story-progress-seg sh-story-progress-seg--done'
                : i === currentIndex.value
                  ? 'sh-story-progress-seg sh-story-progress-seg--active'
                  : 'sh-story-progress-seg'
            }
          />
        ))}
      </div>

      <header class="sh-story-viewer-header">
        <strong>{story.author_user_id}</strong>
        <span class="sh-muted">{story.story_date}</span>
        {!isAuthor && (
          <button
            type="button"
            class="sh-story-viewer-overflow"
            aria-label={`More actions for ${story.author_user_id}`}
            onClick={() => openUserActions(story.author_user_id)}
          >
            ⋯
          </button>
        )}
        <Button variant="ghost" onClick={() => loc.route('/stories')}>Close</Button>
      </header>

      <div class="sh-story-frame">
        <button class="sh-story-tap-left"  onClick={onTapLeft}  aria-label="Previous frame" />
        <button class="sh-story-tap-right" onClick={onTapRight} aria-label="Next frame" />
        {frame.frame_type === 'image' ? (
          <img src={frame.media_url} alt={frame.caption_text ?? ''} class="sh-story-frame-media" />
        ) : (
          <video
            src={frame.media_url}
            class="sh-story-frame-media"
            autoPlay
            playsInline
            muted={false}
            controls={false}
            onEnded={advance}
          />
        )}
        {(frame.caption_text || frame.caption_emoji) && (
          <p class="sh-story-frame-caption">
            {frame.caption_emoji && (
              <span class="sh-story-frame-caption-emoji" aria-hidden="true">
                {frame.caption_emoji}
              </span>
            )}
            {frame.caption_text}
          </p>
        )}
      </div>

      <footer class="sh-story-viewer-footer">
        {!isAuthor && (
          <>
            <button
              type="button"
              class="sh-story-react-btn"
              onClick={() => { reactionsOpen.value = !reactionsOpen.value }}
              aria-label="React"
            >
              😊 React
            </button>
            <button
              type="button"
              class="sh-story-reply-btn"
              onClick={dmReply}
              aria-label="Reply to this frame"
            >
              💬 Reply
            </button>
          </>
        )}
        {isAuthor && (
          <>
            <span class="sh-story-author-meta">
              👁 {myViews.length} · {myReactions.length} reactions
            </span>
            <Button variant="danger" onClick={deleteFrame}>Delete frame</Button>
          </>
        )}
      </footer>

      {reactionsOpen.value && (
        <div class="sh-story-react-tray" role="group" aria-label="Quick reactions">
          {QUICK_REACTIONS.map(emoji => (
            <button
              key={emoji}
              type="button"
              class="sh-story-react-tray-btn"
              onClick={() => void react(emoji)}
            >
              {emoji}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
