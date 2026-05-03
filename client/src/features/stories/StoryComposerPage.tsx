/**
 * StoryComposerPage — pick media + caption + audience and post a frame.
 *
 * v1 keeps the surface tight: one image or short video at a time
 * (subsequent same-day posts append to today's story automatically).
 * The audience picker defaults to the author's preferences (which the
 * Settings page maintains); the user can narrow per-post via the
 * "Audience" dropdown — household tile by default, "Advanced" reveals
 * a per-person multi-select fed by the existing connections endpoint.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import type { StoryAudienceKind } from '@/types'

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

const mediaUrl = signal<string>('')
const mediaPreview = signal<string>('')
const mediaType = signal<'image' | 'video'>('image')
const captionText = signal<string>('')
const captionEmoji = signal<string>('')
const audienceKind = signal<StoryAudienceKind>('all_paired')
const audienceIds = signal<string[]>([])
const submitting = signal<boolean>(false)
const advanced = signal<boolean>(false)
const households = signal<RemoteHousehold[]>([])
const people = signal<ConnectedPerson[]>([])

const QUICK_EMOJIS = ['🌅', '🎉', '🏖', '🍕', '☕', '🐶', '✨', '🥳']


export default function StoryComposerPage() {
  const loc = useLocation()

  useEffect(() => {
    // Reset state on mount.
    mediaUrl.value = ''
    mediaPreview.value = ''
    captionText.value = ''
    captionEmoji.value = ''
    audienceKind.value = 'all_paired'
    audienceIds.value = []
    advanced.value = false

    // Lazy-load connected peers for the picker. Both endpoints exist
    // already; if either fails we silently degrade to "all paired".
    api.get('/api/instances?status=confirmed').then((rows: RemoteHousehold[]) => {
      households.value = rows ?? []
    }).catch(() => {})
    api.get('/api/connections/people').then((rows: ConnectedPerson[]) => {
      people.value = rows ?? []
    }).catch(() => {})
  }, [])

  const onPickMedia = async (e: Event) => {
    const input = e.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return
    const fd = new FormData()
    fd.append('file', file)
    try {
      const r = await fetch('/api/media/upload', {
        method: 'POST',
        body: fd,
        credentials: 'include',
      })
      if (!r.ok) throw new Error(await r.text())
      const data = await r.json() as MediaUploadResponse
      mediaUrl.value = data.url
      mediaPreview.value = data.signed_url
      mediaType.value = file.type.startsWith('video/') ? 'video' : 'image'
    } catch (err: unknown) {
      showToast(`Upload failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  const toggleId = (id: string) => {
    const set = new Set(audienceIds.value)
    if (set.has(id)) set.delete(id); else set.add(id)
    audienceIds.value = Array.from(set)
  }

  const submit = async (e: Event) => {
    e.preventDefault()
    if (!mediaUrl.value || submitting.value) return
    submitting.value = true
    try {
      const body = {
        media_url: mediaUrl.value,
        frame_type: mediaType.value,
        caption_text: captionText.value || null,
        caption_emoji: captionEmoji.value || null,
        audience_kind: audienceKind.value,
        audience: audienceKind.value === 'all_paired' ? [] : audienceIds.value,
      }
      const r = await api.post('/api/stories/frames', body) as
        { story: { id: string }; frame: { id: string } }
      showToast('Story posted', 'success')
      loc.route(`/stories/${r.story.id}`)
    } catch (err: unknown) {
      showToast(`Post failed: ${(err as Error)?.message ?? err}`, 'error')
      submitting.value = false
    }
  }

  return (
    <form class="sh-form sh-story-composer" onSubmit={submit}>
      <header class="sh-stories-header">
        <h2>New story</h2>
        <a href="/stories" class="sh-link">Cancel</a>
      </header>

      <label>
        Media
        <input type="file" accept="image/*,video/*" onChange={onPickMedia} />
      </label>
      {mediaPreview.value && mediaType.value === 'image' && (
        <img src={mediaPreview.value} alt="" class="sh-story-composer-preview" />
      )}
      {mediaPreview.value && mediaType.value === 'video' && (
        <video src={mediaPreview.value} class="sh-story-composer-preview"
          controls playsInline />
      )}

      <label>
        Caption
        <input
          type="text"
          maxLength={140}
          value={captionText.value}
          onInput={e => { captionText.value = (e.target as HTMLInputElement).value }}
          placeholder="A line for this moment..."
        />
      </label>

      <fieldset class="sh-story-composer-emoji">
        <legend class="sh-muted">Emoji</legend>
        <div class="sh-story-composer-emoji-row" role="radiogroup">
          {QUICK_EMOJIS.map(e => (
            <button
              key={e}
              type="button"
              role="radio"
              aria-checked={captionEmoji.value === e}
              class={
                captionEmoji.value === e
                  ? 'sh-story-composer-emoji-btn sh-story-composer-emoji-btn--active'
                  : 'sh-story-composer-emoji-btn'
              }
              onClick={() => {
                captionEmoji.value = captionEmoji.value === e ? '' : e
              }}
            >
              {e}
            </button>
          ))}
        </div>
      </fieldset>

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
          disabled={!mediaUrl.value || submitting.value}
        >
          Post story
        </Button>
      </div>
    </form>
  )
}
