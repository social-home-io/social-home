/**
 * StoryComposerPage — pick media + caption + audience and post one
 * or more frames as a single story.
 *
 * Match the WhatsApp-Status experience: pick several photos / videos
 * in one shot, write a caption per frame, hit Post. The server's
 * ``POST /api/stories/frames`` route already creates-or-appends to
 * today's story keyed by ``(author_user_id, story_date)``, so the
 * submit path just iterates over the staged frames sequentially.
 *
 * The first frame carries the audience (story-level); later frames
 * inherit it server-side. Per-frame ``caption_text`` is supported by
 * the schema today; the legacy ``caption_emoji`` field is no longer
 * surfaced in the UI — the per-frame :class:`EmojiPickButton` splices
 * glyphs straight into the caption text.
 */
import { useEffect, useRef } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { EmojiPickButton } from '@/components/EmojiPickButton'
import { showToast } from '@/components/Toast'
import { describeUploadError } from '@/utils/uploadErrors'
import { currentUser } from '@/store/auth'
import type { StoryAudienceKind, StoryInboxItem } from '@/types'

interface MediaUploadResponse {
  url: string
  signed_url: string
  filename: string
}

interface RemoteHousehold {
  instance_id: string
  display_name: string
}

interface ConnectedPerson {
  user_id: string
  display_name: string
  instance_id: string
}

/** A media file the user has uploaded but not yet posted. Each entry
 *  becomes one frame on submit. */
interface StagedFrame {
  /** Local-only id for the keyed render + ref map. */
  id:       string
  /** Canonical ``/api/media/{filename}`` URL that lands in the post. */
  url:      string
  /** Short-lived signed URL for the local ``<img>`` / ``<video>`` preview. */
  preview:  string
  type:     'image' | 'video'
  /** Original filename — used for the remove × aria-label. */
  name:     string
  /** Per-frame caption (140 chars max). */
  caption:  string
}

const CAPTION_MAX = 140
/** Server-enforced cap (``MAX_FRAMES_PER_STORY`` in
 *  ``socialhome/services/story_service.py``). Surfaced here so a
 *  multi-pick can refuse the overflow before the upload starts. */
const MAX_FRAMES_PER_STORY = 30

const stagedFrames = signal<StagedFrame[]>([])
/** Frames already posted on today's story by the current user — read
 *  once on mount so the picker can refuse over the daily cap. */
const mineToday = signal<number>(0)
const audienceKind = signal<StoryAudienceKind>('all_paired')
const audienceIds = signal<string[]>([])
const submitting = signal<boolean>(false)
const advanced = signal<boolean>(false)
const households = signal<RemoteHousehold[]>([])
const people = signal<ConnectedPerson[]>([])


export default function StoryComposerPage() {
  const loc = useLocation()
  // Per-frame textarea refs so the emoji picker can splice at the
  // caret rather than the end. Keyed by the local-only ``StagedFrame.id``.
  const captionRefs = useRef(new Map<string, HTMLTextAreaElement>())

  useEffect(() => {
    // Reset state on mount.
    stagedFrames.value = []
    audienceKind.value = 'all_paired'
    audienceIds.value = []
    advanced.value = false
    mineToday.value = 0

    // Lazy-load connected peers for the picker. Both endpoints exist
    // already; if either fails we silently degrade to "all paired".
    api.get('/api/instances?status=confirmed').then((rows: RemoteHousehold[]) => {
      households.value = rows ?? []
    }).catch(() => {})
    api.get('/api/connections/people').then((rows: ConnectedPerson[]) => {
      people.value = rows ?? []
    }).catch(() => {})

    // Today's frame count for the cap. Authors can post up to
    // ``MAX_FRAMES_PER_STORY`` frames per day; the server returns
    // ``STORY_FRAME_LIMIT`` past that. Surface the number locally so
    // multi-pick refuses the overflow without an upload round-trip.
    api.get('/api/stories').then((rows: StoryInboxItem[]) => {
      const me = currentUser.value?.user_id
      if (!me) return
      const todayKey = new Date().toISOString().slice(0, 10)
      const mine = (rows ?? []).find(
        s => s.story.author_user_id === me && s.story.story_date === todayKey,
      )
      mineToday.value = mine?.frames.length ?? 0
    }).catch(() => {})
  }, [])

  const framesLeft = (): number => Math.max(
    0, MAX_FRAMES_PER_STORY - mineToday.value - stagedFrames.value.length,
  )

  const uploadOne = async (file: File): Promise<StagedFrame | null> => {
    const fd = new FormData()
    fd.append('file', file)
    try {
      const r = await fetch('/api/media/upload', {
        method:      'POST',
        body:        fd,
        credentials: 'include',
      })
      if (!r.ok) throw new Error(await r.text())
      const data = await r.json() as MediaUploadResponse
      return {
        id:      crypto.randomUUID(),
        url:     data.url,
        preview: data.signed_url,
        type:    file.type.startsWith('video/') ? 'video' : 'image',
        name:    file.name,
        caption: '',
      }
    } catch (err: unknown) {
      showToast(describeUploadError(err, { file }), 'error')
      return null
    }
  }

  const acceptFiles = async (files: File[]): Promise<void> => {
    if (files.length === 0) return
    const left = framesLeft()
    if (left <= 0) {
      showToast(
        `You've reached today's limit of ${MAX_FRAMES_PER_STORY} story frames.`,
        'error',
      )
      return
    }
    const accepted = files.slice(0, left)
    if (files.length > accepted.length) {
      showToast(
        `You can add ${left} more frame${left === 1 ? '' : 's'} to today's story.`,
        'info',
      )
    }
    for (const f of accepted) {
      const staged = await uploadOne(f)
      if (staged) {
        stagedFrames.value = [...stagedFrames.value, staged]
      }
    }
  }

  const onPickMedia = async (e: Event) => {
    const input = e.target as HTMLInputElement
    await acceptFiles(Array.from(input.files ?? []))
    // Reset so re-picking the same file fires onchange again.
    input.value = ''
  }

  const removeFrame = (id: string) => {
    stagedFrames.value = stagedFrames.value.filter(f => f.id !== id)
    captionRefs.current.delete(id)
  }

  const updateCaption = (id: string, value: string) => {
    stagedFrames.value = stagedFrames.value.map(f =>
      f.id === id ? { ...f, caption: value.slice(0, CAPTION_MAX) } : f,
    )
  }

  const spliceEmojiIntoFrame = (frameId: string, emoji: string) => {
    const ta = captionRefs.current.get(frameId)
    const idx = stagedFrames.value.findIndex(f => f.id === frameId)
    if (idx < 0) return
    const cur = stagedFrames.value[idx].caption
    const start = ta?.selectionStart ?? cur.length
    const end   = ta?.selectionEnd   ?? start
    const next = (cur.slice(0, start) + emoji + cur.slice(end)).slice(0, CAPTION_MAX)
    stagedFrames.value = stagedFrames.value.map((f, i) =>
      i === idx ? { ...f, caption: next } : f,
    )
    if (ta) {
      requestAnimationFrame(() => {
        ta.focus()
        const pos = (cur.slice(0, start) + emoji).length
        ta.setSelectionRange(pos, pos)
      })
    }
  }

  const toggleId = (id: string) => {
    const set = new Set(audienceIds.value)
    if (set.has(id)) set.delete(id); else set.add(id)
    audienceIds.value = Array.from(set)
  }

  const submit = async (e: Event) => {
    e.preventDefault()
    const frames = stagedFrames.value
    if (frames.length === 0 || submitting.value) return
    submitting.value = true
    let lastStoryId: string | null = null
    for (let i = 0; i < frames.length; i++) {
      const f = frames[i]
      try {
        const r = await api.post('/api/stories/frames', {
          media_url:    f.url,
          frame_type:   f.type,
          caption_text: f.caption.trim() || null,
          // Audience rides on the first frame only — later frames
          // inherit the story-level audience server-side.
          audience_kind: i === 0 ? audienceKind.value : undefined,
          audience:      i === 0 && audienceKind.value !== 'all_paired'
            ? audienceIds.value : [],
        }) as { story: { id: string }; frame: { id: string } }
        lastStoryId = r.story.id
      } catch (err: unknown) {
        showToast(
          `Frame ${i + 1} failed: ${(err as Error)?.message ?? err}`,
          'error',
        )
        // Stop the loop with already-posted frames intact; the user
        // can navigate to the partial story and decide.
        break
      }
    }
    if (lastStoryId) {
      showToast(
        frames.length === 1 ? 'Story posted' : `Posted ${frames.length} frames`,
        'success',
      )
      loc.route(`/stories/${lastStoryId}`)
    } else {
      submitting.value = false
    }
  }

  const left = framesLeft()
  const canPickMore = left > 0

  return (
    <form class="sh-form sh-story-composer" onSubmit={submit}>
      <header class="sh-stories-header">
        <h2>New story</h2>
        <a href="/stories" class="sh-link">Cancel</a>
      </header>

      <label>
        Media
        <input
          type="file"
          accept="image/*,video/*"
          multiple
          disabled={!canPickMore}
          onChange={onPickMedia}
        />
      </label>
      <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
        {canPickMore
          ? `You can add up to ${left} more frame${left === 1 ? '' : 's'} to today's story.`
          : `You've reached today's limit of ${MAX_FRAMES_PER_STORY} frames.`}
      </p>

      {stagedFrames.value.length > 0 && (
        <ol class="sh-story-frames">
          {stagedFrames.value.map((f, i) => (
            <li key={f.id} class="sh-story-frame">
              <div class="sh-story-frame-thumb">
                {f.type === 'image' ? (
                  <img src={f.preview} alt="" />
                ) : (
                  <video src={f.preview} muted preload="metadata" />
                )}
              </div>
              <div class="sh-story-frame-body">
                <div class="sh-story-frame-meta">
                  <strong>Frame {i + 1} of {stagedFrames.value.length}</strong>
                  <button
                    type="button"
                    class="sh-link sh-story-frame-remove"
                    aria-label={`Remove ${f.name}`}
                    onClick={() => removeFrame(f.id)}
                  >✕</button>
                </div>
                <div class="sh-story-frame-caption-row">
                  <textarea
                    ref={(el: HTMLTextAreaElement | null) => {
                      if (el) captionRefs.current.set(f.id, el)
                      else captionRefs.current.delete(f.id)
                    }}
                    rows={2}
                    maxLength={CAPTION_MAX}
                    placeholder="A line for this moment…"
                    value={f.caption}
                    onInput={e => updateCaption(
                      f.id, (e.target as HTMLTextAreaElement).value,
                    )}
                  />
                  <EmojiPickButton
                    openKey={`story-frame-${f.id}`}
                    ariaLabel={`Add emoji to frame ${i + 1}`}
                    onInsert={(emoji) => spliceEmojiIntoFrame(f.id, emoji)}
                  />
                </div>
              </div>
            </li>
          ))}
        </ol>
      )}

      <fieldset class="sh-story-composer-audience">
        <legend class="sh-muted">Audience</legend>
        <label class="sh-story-composer-audience-row">
          <input
            type="radio"
            name="audience"
            checked={audienceKind.value === 'all_paired'}
            onChange={() => {
              audienceKind.value = 'all_paired'
              audienceIds.value = []
            }}
          />
          All connected households (default)
        </label>
        <label class="sh-story-composer-audience-row">
          <input
            type="radio"
            name="audience"
            checked={audienceKind.value === 'households'}
            onChange={() => {
              audienceKind.value = 'households'
              audienceIds.value = []
            }}
          />
          Pick households
        </label>
        {audienceKind.value === 'households' && (
          <div class="sh-story-composer-audience-list">
            {households.value.length === 0 && (
              <p class="sh-muted">No connected households yet.</p>
            )}
            {households.value.map(h => (
              <label key={h.instance_id} class="sh-story-composer-audience-row">
                <input
                  type="checkbox"
                  checked={audienceIds.value.includes(h.instance_id)}
                  onChange={() => toggleId(h.instance_id)}
                />
                {h.display_name}
              </label>
            ))}
          </div>
        )}
        <button
          type="button"
          class="sh-link sh-story-composer-advanced-toggle"
          onClick={() => { advanced.value = !advanced.value }}
        >
          {advanced.value ? 'Hide' : 'Show'} per-person picker (advanced)
        </button>
        {advanced.value && (
          <>
            <label class="sh-story-composer-audience-row">
              <input
                type="radio"
                name="audience"
                checked={audienceKind.value === 'users'}
                onChange={() => {
                  audienceKind.value = 'users'
                  audienceIds.value = []
                }}
              />
              Pick people
            </label>
            {audienceKind.value === 'users' && (
              <div class="sh-story-composer-audience-list">
                {people.value.length === 0 && (
                  <p class="sh-muted">No connected people yet.</p>
                )}
                {people.value.map(p => (
                  <label key={p.user_id} class="sh-story-composer-audience-row">
                    <input
                      type="checkbox"
                      checked={audienceIds.value.includes(p.user_id)}
                      onChange={() => toggleId(p.user_id)}
                    />
                    {p.display_name}
                    <span class="sh-muted"> @ {p.instance_id.slice(0, 8)}…</span>
                  </label>
                ))}
              </div>
            )}
          </>
        )}
      </fieldset>

      <div class="sh-form-actions">
        <Button
          type="submit"
          loading={submitting.value}
          disabled={stagedFrames.value.length === 0 || submitting.value}
        >
          {stagedFrames.value.length > 1
            ? `Post ${stagedFrames.value.length} frames`
            : 'Post story'}
        </Button>
      </div>
    </form>
  )
}
