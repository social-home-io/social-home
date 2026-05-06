/**
 * StoryPublishMenu — author-only modal to publish a story to a paired
 * GFS and copy the public link (§stories_public).
 *
 * Opens from a "Publish public link" button in the Stories viewer
 * footer. Author picks a connected GFS, optionally labels the link
 * (e.g. "for-twitter"), and the modal calls
 * ``POST /api/stories/{id}/publish``. The returned URL is shown with
 * a copy button. Author can also tap "Unpublish all" to revoke every
 * token under this story.
 *
 * Once the link is minted, the modal exposes an optional preview-
 * thumbnail upload (§stories_public OG card). The thumbnail is
 * cached on the GFS so anonymous social-card crawlers (Twitter,
 * Slack, iMessage) render a real preview alongside the share link.
 *
 * Token list lives on the GFS — listing existing tokens is intentional
 * future work; v1 surface is "publish (mints a token), unpublish (drops
 * them all)".
 */
import { useEffect, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Modal } from '@/components/Modal'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'

interface GfsConnection {
  id: string
  display_name: string
  status: string
}

interface PublishedToken {
  url: string
  token: string
  label: string | null
}

const open = signal(false)
const storyId = signal('')
const connections = signal<GfsConnection[]>([])
const selectedGfs = signal('')
const label = signal('')
const submitting = signal(false)
const lastIssued = signal<PublishedToken | null>(null)
const isPublished = signal(false)

export function openPublishMenu(id: string, alreadyPublished: boolean): void {
  storyId.value = id
  isPublished.value = alreadyPublished
  label.value = ''
  lastIssued.value = null
  open.value = true
}

export function StoryPublishMenu() {
  useEffect(() => {
    if (!open.value) return
    void api.get<{ connections: GfsConnection[] }>('/api/gfs/connections')
      .then((res) => {
        const active = (res.connections ?? []).filter(c => c.status === 'active')
        connections.value = active
        if (active.length === 1) selectedGfs.value = active[0].id
      })
      .catch(() => { connections.value = [] })
  }, [open.value])

  const submit = async () => {
    if (submitting.value || !selectedGfs.value) return
    submitting.value = true
    try {
      const res = await api.post<PublishedToken>(
        `/api/stories/${storyId.value}/publish`,
        { gfs_id: selectedGfs.value, label: label.value || undefined },
      )
      lastIssued.value = res
      isPublished.value = true
      showToast('Public link minted', 'success')
    } catch (err: unknown) {
      showToast(`Couldn't publish: ${(err as Error)?.message ?? err}`, 'error')
    } finally {
      submitting.value = false
    }
  }

  const copy = async () => {
    if (!lastIssued.value) return
    try {
      await navigator.clipboard.writeText(lastIssued.value.url)
      showToast('Link copied', 'success')
    } catch {
      showToast('Copy failed — long-press the link to copy manually', 'info')
    }
  }

  const unpublishAll = async () => {
    if (submitting.value) return
    submitting.value = true
    try {
      await api.delete(`/api/stories/${storyId.value}/publish`)
      isPublished.value = false
      lastIssued.value = null
      showToast('Public link removed', 'info')
    } catch (err: unknown) {
      showToast(`Couldn't unpublish: ${(err as Error)?.message ?? err}`, 'error')
    } finally {
      submitting.value = false
    }
  }

  return (
    <Modal
      open={open.value}
      onClose={() => { open.value = false }}
      title="Share this story publicly"
    >
      <p class="sh-muted">
        Mint a link anyone can open in a browser. The story streams
        directly from your home server — the relay only brokers the
        handshake. The link stops working when the story expires, or
        if you tap unpublish.
      </p>

      {connections.value.length === 0 && (
        <p class="sh-muted">
          You're not connected to any Global Federation Server yet.
          Connect one in <a href="/settings/connections">Settings → Connections</a>.
        </p>
      )}

      {connections.value.length > 0 && !lastIssued.value && (
        <form
          class="sh-story-publish-form"
          onSubmit={(e) => { e.preventDefault(); void submit() }}
        >
          <label class="sh-form-row">
            <span>Relay</span>
            {connections.value.length === 1 ? (
              <input
                type="text"
                value={connections.value[0].display_name}
                disabled
              />
            ) : (
              <select
                value={selectedGfs.value}
                onChange={(e) => {
                  selectedGfs.value = (e.currentTarget as HTMLSelectElement).value
                }}
              >
                <option value="">Pick a relay…</option>
                {connections.value.map(c => (
                  <option key={c.id} value={c.id}>{c.display_name}</option>
                ))}
              </select>
            )}
          </label>
          <label class="sh-form-row">
            <span>Label (optional)</span>
            <input
              type="text"
              maxLength={64}
              placeholder="e.g. twitter"
              value={label.value}
              onInput={(e) => {
                label.value = (e.currentTarget as HTMLInputElement).value
              }}
            />
          </label>
          <div class="sh-modal-actions">
            <Button
              type="submit"
              disabled={submitting.value || !selectedGfs.value}
            >
              {submitting.value ? 'Publishing…' : 'Mint link'}
            </Button>
          </div>
        </form>
      )}

      {lastIssued.value && (
        <div class="sh-story-publish-issued">
          <p><strong>Your link:</strong></p>
          <code class="sh-story-publish-url">{lastIssued.value.url}</code>
          <div class="sh-modal-actions">
            <Button onClick={copy}>Copy</Button>
          </div>
          <OgThumbnailUpload storyId={storyId.value} />
        </div>
      )}

      {(isPublished.value || lastIssued.value) && (
        <div class="sh-modal-actions sh-modal-actions--secondary">
          <Button
            variant="danger"
            onClick={unpublishAll}
            disabled={submitting.value}
          >
            Unpublish all links for this story
          </Button>
        </div>
      )}
    </Modal>
  )
}


/**
 * OgThumbnailUpload — optional preview image for the public link.
 *
 * Shown after a successful publish. Author picks a JPEG / PNG; we
 * decode it to a JPEG via canvas (so we always upload the same wire
 * format the GFS expects), base64-encode, and POST to
 * ``/api/stories/{id}/publish/og``. The GFS caches the bytes for
 * anonymous OG crawlers — no token needed to fetch the preview.
 */
function OgThumbnailUpload({ storyId }: { storyId: string }) {
  const [busy, setBusy] = useState(false)
  const [uploaded, setUploaded] = useState<string | null>(null)

  const onFile = async (file: File) => {
    setBusy(true)
    try {
      const b64 = await fileToJpegBase64(file)
      const res = await api.post<{ url: string }>(
        `/api/stories/${storyId}/publish/og`,
        { image_b64: b64 },
      )
      setUploaded(res.url)
      showToast('Preview image uploaded', 'success')
    } catch (err: unknown) {
      showToast(
        `Couldn't upload preview: ${(err as Error)?.message ?? err}`,
        'error',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div class="sh-story-publish-og">
      <p class="sh-muted">
        Optional: upload a preview image to show on Twitter / iMessage /
        Slack when someone shares this link. The image is cached on
        the relay (the rest of the story still streams from your home
        server). 200 KB max.
      </p>
      <input
        type="file"
        accept="image/*"
        disabled={busy}
        onChange={(e) => {
          const file = (e.currentTarget as HTMLInputElement).files?.[0]
          if (file) void onFile(file)
        }}
      />
      {busy && <span class="sh-muted"> Uploading…</span>}
      {uploaded && (
        <p class="sh-muted">
          Preview live at <a href={uploaded}>{uploaded}</a>.
        </p>
      )}
    </div>
  )
}


/**
 * Decode the user's image, render to a 1200×630 canvas (the size
 * Twitter / iMessage cards prefer), encode as JPEG, return the
 * base64 payload. Keeps the wire format predictable on the GFS side
 * regardless of what the author picked locally.
 */
async function fileToJpegBase64(file: File): Promise<string> {
  const bitmap = await createImageBitmap(file)
  const W = 1200, H = 630
  const canvas = document.createElement('canvas')
  canvas.width = W
  canvas.height = H
  const ctx = canvas.getContext('2d')!
  // Cover-fit: scale so the source covers the canvas, then centre-crop.
  const scale = Math.max(W / bitmap.width, H / bitmap.height)
  const dw = bitmap.width * scale
  const dh = bitmap.height * scale
  const dx = (W - dw) / 2
  const dy = (H - dh) / 2
  ctx.fillStyle = '#000'
  ctx.fillRect(0, 0, W, H)
  ctx.drawImage(bitmap, dx, dy, dw, dh)
  const blob: Blob = await new Promise((resolve, reject) => {
    canvas.toBlob(
      (b) => (b ? resolve(b) : reject(new Error('JPEG encode failed'))),
      'image/jpeg',
      0.85,
    )
  })
  const buf = await blob.arrayBuffer()
  // Base64-encode the bytes.
  const bytes = new Uint8Array(buf)
  let bin = ''
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i])
  return btoa(bin)
}
