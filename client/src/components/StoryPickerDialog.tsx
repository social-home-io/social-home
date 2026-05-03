/**
 * StoryPickerDialog — pick one of your active stories to share into a
 * household / space feed (§Stories).
 *
 * Driven by a single global signal so any composer surface can flip it
 * open and pass the destination scope. Submission POSTs
 * ``/api/stories/{id}/share`` and the resulting feed-post id is
 * returned to the caller via the ``onShared`` callback so the surface
 * can refresh its feed inline.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Modal } from './Modal'
import { Button } from './Button'
import { showToast } from './Toast'
import { currentUser } from '@/store/auth'
import type { StoryInboxItem } from '@/types'

const open = signal(false)
const scope = signal<'household' | 'space'>('household')
const spaceId = signal<string | null>(null)
const note = signal<string>('')
const items = signal<StoryInboxItem[]>([])
const loading = signal<boolean>(false)
const submittingId = signal<string | null>(null)
let onSharedCb: ((postId: string) => void) | null = null


export interface OpenStoryPickerOptions {
  scope: 'household' | 'space'
  spaceId?: string | null
  onShared?: (postId: string) => void
}


export function openStoryPicker(opts: OpenStoryPickerOptions): void {
  scope.value = opts.scope
  spaceId.value = opts.spaceId ?? null
  note.value = ''
  items.value = []
  loading.value = true
  submittingId.value = null
  onSharedCb = opts.onShared ?? null
  open.value = true
  api.get('/api/stories')
    .then((rows: StoryInboxItem[]) => {
      // Only the user's own stories are sharable — re-sharing someone
      // else's content beyond the audience they picked would be a
      // privacy footgun.
      const me = currentUser.value?.user_id
      items.value = (rows ?? []).filter(r => r.story.author_user_id === me)
    })
    .catch(() => {
      items.value = []
    })
    .finally(() => {
      loading.value = false
    })
}


export function StoryPickerDialog() {
  useEffect(() => {
    if (!open.value) return
  }, [open.value])

  const submit = async (storyId: string) => {
    if (submittingId.value) return
    submittingId.value = storyId
    try {
      const body: Record<string, unknown> = { scope: scope.value }
      if (scope.value === 'space' && spaceId.value) {
        body.space_id = spaceId.value
      }
      if (note.value.trim()) body.note = note.value.trim()
      const r = await api.post(`/api/stories/${storyId}/share`, body) as
        { post_id?: string; queued?: boolean }
      if (r.queued) {
        showToast('Queued for moderator review', 'info')
      } else {
        showToast('Story shared', 'success')
      }
      open.value = false
      if (r.post_id && onSharedCb) onSharedCb(r.post_id)
    } catch (err: unknown) {
      showToast(`Share failed: ${(err as Error)?.message ?? err}`, 'error')
    } finally {
      submittingId.value = null
    }
  }

  if (!open.value) return null
  return (
    <Modal open={open.value} onClose={() => { open.value = false }} title="Share a story">
      <div class="sh-story-picker">
        {loading.value && <p class="sh-muted">Loading your stories…</p>}
        {!loading.value && items.value.length === 0 && (
          <p class="sh-muted">
            You don't have any active stories yet. Post one from{' '}
            <a href="/stories/new" class="sh-link">Stories → New</a> first.
          </p>
        )}
        {!loading.value && items.value.length > 0 && (
          <>
            <label class="sh-form-row">
              Optional note
              <input
                type="text"
                value={note.value}
                onInput={e => { note.value = (e.target as HTMLInputElement).value }}
                placeholder="Why are you sharing this here?"
                maxLength={140}
              />
            </label>
            <div class="sh-story-picker-grid">
              {items.value.map(item => {
                const first = item.frames[0]
                return (
                  <button
                    key={item.story.id}
                    type="button"
                    class="sh-story-picker-tile"
                    onClick={() => void submit(item.story.id)}
                    disabled={submittingId.value !== null}
                  >
                    {first && first.frame_type === 'image' && (
                      <img
                        src={first.media_url}
                        alt=""
                        class="sh-story-picker-thumb"
                      />
                    )}
                    {first && first.frame_type === 'video' && (
                      <span class="sh-story-picker-thumb sh-story-picker-thumb--video">
                        🎬
                      </span>
                    )}
                    <span class="sh-story-picker-meta">
                      <strong>{item.story.story_date}</strong>
                      <span class="sh-muted">
                        {item.frames.length} frame{item.frames.length === 1 ? '' : 's'}
                      </span>
                    </span>
                  </button>
                )
              })}
            </div>
          </>
        )}
        <div class="sh-form-actions sh-story-picker-actions">
          <Button variant="secondary" onClick={() => { open.value = false }}>
            Cancel
          </Button>
        </div>
      </div>
    </Modal>
  )
}
