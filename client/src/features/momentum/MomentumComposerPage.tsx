/**
 * MomentumComposerPage — create a new moment (§Momentum).
 *
 * Routed at ``/momentum/new`` for top-level posts and
 * ``/momentum/{id}/reply`` for replies. Replies attach to the route
 * id; the server collapses replies-of-replies onto the root.
 *
 * Client-side guards (server re-checks):
 *   - Text capped at 1 000 chars (live counter).
 *   - Video duration capped at 15 s (read on `loadedmetadata` before
 *     the upload form-data is constructed).
 */
import { useEffect, useRef } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation, useRoute } from 'preact-iso'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { useTitle } from '@/store/pageTitle'
import type { Moment } from '@/types'

const MAX_CONTENT = 1_000
const MAX_VIDEO_MS = 15_000

const submitting = signal<boolean>(false)
const content = signal<string>('')
const mediaUrl = signal<string | null>(null)
const mediaType = signal<'image' | 'video' | null>(null)
const durationMs = signal<number | null>(null)


export default function MomentumComposerPage() {
  const { params } = useRoute()
  const loc = useLocation()
  const parentId = params.momentId ?? null
  useTitle(parentId ? 'Reply' : 'New moment')
  const fileRef = useRef<HTMLInputElement>(null)

  // Reset state on mount so navigating from one composer to another
  // (top-level → reply → top-level) doesn't carry stale fields.
  useEffect(() => {
    content.value = ''
    mediaUrl.value = null
    mediaType.value = null
    durationMs.value = null
    submitting.value = false
  }, [parentId])

  const onPick = async () => {
    const f = fileRef.current?.files?.[0]
    if (!f) return
    const isVideo = f.type.startsWith('video/')
    const isImage = f.type.startsWith('image/')
    if (!isVideo && !isImage) {
      showToast('Pick an image or video.', 'error')
      return
    }
    if (isVideo) {
      // Pre-flight duration check before the upload round-trip.
      const url = URL.createObjectURL(f)
      const probe: HTMLVideoElement = document.createElement('video')
      probe.preload = 'metadata'
      probe.src = url
      const dur = await new Promise<number>((resolve, reject) => {
        probe.onloadedmetadata = () => resolve(probe.duration * 1_000)
        probe.onerror = () => reject(new Error('Could not read video metadata'))
      }).finally(() => URL.revokeObjectURL(url))
      if (dur > MAX_VIDEO_MS + 50 /* small slack for sub-frame rounding */) {
        showToast(
          `Videos cap at ${MAX_VIDEO_MS / 1000} seconds. This one is ${(dur / 1000).toFixed(1)} s.`,
          'error',
        )
        return
      }
      durationMs.value = Math.round(dur)
    }
    try {
      const fd = new FormData()
      fd.append('file', f, f.name)
      const r = await api.upload('/api/media/upload', fd) as { url?: string }
      if (!r.url) throw new Error('Upload returned no url')
      mediaUrl.value = r.url
      mediaType.value = isVideo ? 'video' : 'image'
    } catch (err: unknown) {
      showToast(`Upload failed: ${(err as Error)?.message ?? err}`, 'error')
      mediaUrl.value = null
      mediaType.value = null
      durationMs.value = null
    }
  }

  const submit = async () => {
    const text = content.value.trim()
    if (!text && !mediaUrl.value) {
      showToast('Add some text or media first.', 'error')
      return
    }
    if (text.length > MAX_CONTENT) {
      showToast(`Trim to ${MAX_CONTENT} characters.`, 'error')
      return
    }
    submitting.value = true
    try {
      const body: Record<string, unknown> = { content: text }
      if (mediaUrl.value) {
        body.media_url = mediaUrl.value
        body.media_type = mediaType.value
        if (mediaType.value === 'video') body.duration_ms = durationMs.value
      }
      if (parentId) body.parent_moment_id = parentId
      const m = await api.post('/api/moments', body) as Moment
      loc.route(parentId ? `/momentum/${parentId}` : `/momentum/${m.id}`)
    } catch (err: unknown) {
      const msg = (err as Error)?.message ?? String(err)
      // 429 carries a friendly message body — surface that instead.
      showToast(`Couldn't post: ${msg}`, 'error')
      submitting.value = false
    }
  }

  const remaining = MAX_CONTENT - content.value.length

  return (
    <div class="sh-momentum-composer">
      <header class="sh-momentum-composer-header">
        <h2>{parentId ? 'Reply' : 'New moment'}</h2>
        <Button variant="ghost" onClick={() => loc.route('/momentum')}>Cancel</Button>
      </header>

      {parentId && (
        <p class="sh-muted">
          Replying to moment <code>{parentId.slice(0, 8)}…</code>
        </p>
      )}

      <textarea
        class="sh-momentum-composer-text"
        rows={5}
        maxLength={MAX_CONTENT}
        placeholder="Share a moment…"
        value={content.value}
        onInput={(e) => { content.value = (e.target as HTMLTextAreaElement).value }}
      />
      <div class="sh-momentum-composer-meta">
        <span class={remaining < 0 ? 'sh-error' : 'sh-muted'}>
          {remaining} characters left
        </span>
      </div>

      <div class="sh-momentum-composer-media">
        <input
          ref={fileRef}
          type="file"
          accept="image/*,video/*"
          onChange={() => void onPick()}
        />
        {mediaUrl.value && mediaType.value === 'image' && (
          <img src={mediaUrl.value} alt="" class="sh-momentum-composer-preview" />
        )}
        {mediaUrl.value && mediaType.value === 'video' && (
          <video
            src={mediaUrl.value}
            class="sh-momentum-composer-preview"
            controls
            muted
            preload="metadata"
          />
        )}
      </div>

      <div class="sh-momentum-composer-actions">
        <Button onClick={submit} disabled={submitting.value}>
          {submitting.value ? 'Posting…' : (parentId ? 'Reply' : 'Post moment')}
        </Button>
      </div>
    </div>
  )
}
