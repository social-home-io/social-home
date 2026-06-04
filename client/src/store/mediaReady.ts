/**
 * mediaReady store — tracks transcoded video output filenames the
 * backend has flagged as finished via the ``media.ready`` WS frame.
 *
 * Video uploads transcode asynchronously: a list payload may carry a
 * video item with ``media_status: 'processing'`` while the worker is
 * still encoding. When transcoding finishes the backend pushes a
 * ``media.ready`` frame to the uploader carrying the output filename
 * (``<uuid>.webm``). Any mounted :mod:`VideoMedia` keyed on that
 * filename then swaps from its "Processing…" placeholder to the real
 * player — without a refetch — because it reads :data:`readyMedia`
 * inside its render body (signal subscription).
 *
 * Keyed by output *filename* (the last path segment before ``?``) so a
 * signed URL (``api/media/<uuid>.webm?exp=&sig=``) and the bare frame
 * filename resolve to the same key.
 */
import { signal } from '@preact/signals'
import { ws } from '@/ws'

// Output filenames (e.g. "abc.webm") the server told us finished transcoding.
export const readyMedia = signal<Set<string>>(new Set())

// Output filenames the server told us permanently failed transcoding via a
// ``media.failed`` WS frame. A mounted VideoMedia keyed on the filename flips
// to its failed state without a refetch (it reads this signal in its body).
export const failedMedia = signal<Set<string>>(new Set())

/** Mark a transcoded output filename as ready (idempotent). */
export function markMediaReady(filename: string): void {
  if (!filename || readyMedia.value.has(filename)) return
  const next = new Set(readyMedia.value)
  next.add(filename)
  readyMedia.value = next
}

/** Mark a transcoded output filename as permanently failed (idempotent). */
export function markMediaFailed(filename: string): void {
  if (!filename || failedMedia.value.has(filename)) return
  const next = new Set(failedMedia.value)
  next.add(filename)
  failedMedia.value = next
}

/** Extract the bare filename (drop query string + path) from a media URL. */
export function mediaFilename(url: string | null | undefined): string {
  if (!url) return ''
  return url.split('?', 1)[0].split('/').filter(Boolean).pop() ?? ''
}

/**
 * Subscribe the store to ``media.ready`` / ``media.failed`` frames. Call
 * once at app start.
 */
export function wireMediaReadyWs(): void {
  ws.on('media.ready', (e) => {
    const data = e.data as { output_filename?: string }
    markMediaReady(data.output_filename ?? '')
  })
  ws.on('media.failed', (e) => {
    const data = e.data as { output_filename?: string }
    markMediaFailed(data.output_filename ?? '')
  })
}

export function _resetMediaReadyForTest(): void {
  readyMedia.value = new Set()
  failedMedia.value = new Set()
}
