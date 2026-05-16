/* eslint-disable @typescript-eslint/no-explicit-any */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import {
  uploadWithProgress,
  uploadProgress,
  type UploadEvent,
} from './UploadProgress'

// ── Fake XMLHttpRequest ────────────────────────────────────────────────
//
// We rebuild just enough of the XHR surface to drive
// ``uploadWithProgress`` deterministically — ``upload.onprogress``,
// ``upload.onload`` (body-fully-sent), ``onload`` (response landed),
// ``onerror``, plus the status / responseText accessors the success
// path reads.

class _FakeXHR {
  upload = {
    onprogress: null as ((e: { lengthComputable: boolean; loaded: number; total: number }) => void) | null,
    onload: null as (() => void) | null,
  }
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  status = 0
  responseText = ''
  _headers: Record<string, string> = {}
  open(_method: string, _url: string) { /* noop */ }
  setRequestHeader(k: string, v: string) { this._headers[k] = v }
  send(_body: FormData) { /* noop — tests drive the lifecycle by hand */ }

  // Test hooks: deliver phases to the consumer.
  _sendProgress(percent: number, total = 100) {
    this.upload.onprogress?.({ lengthComputable: true, loaded: percent, total })
  }
  _bodyFullySent() { this.upload.onload?.() }
  _respondOk(body: object) {
    this.status = 200
    this.responseText = JSON.stringify(body)
    this.onload?.()
  }
  _respondFail(status: number) {
    this.status = status
    this.responseText = ''
    this.onload?.()
  }
  _networkError() { this.onerror?.() }
}

let lastXhr: _FakeXHR | null = null

beforeEach(() => {
  uploadProgress.value = null
  lastXhr = null
  ;(globalThis as any).XMLHttpRequest = function () {
    lastXhr = new _FakeXHR()
    return lastXhr
  } as any
})

afterEach(() => {
  delete (globalThis as any).XMLHttpRequest
})

describe('uploadWithProgress', () => {
  it('emits uploading events at every progress tick', async () => {
    const events: UploadEvent[] = []
    const file = new File(['x'], 'photo.jpg', { type: 'image/jpeg' })
    const p = uploadWithProgress(file, (e) => events.push(e))
    // Simulate two progress ticks.
    lastXhr!._sendProgress(50)
    lastXhr!._sendProgress(99)
    lastXhr!._bodyFullySent()
    lastXhr!._respondOk({ url: 'api/media/x.webp', filename: 'x.webp' })
    await p

    const phases = events.map(e => e.phase)
    expect(phases).toEqual(['uploading', 'uploading', 'processing', 'done'])
    expect(events[0].percent).toBe(50)
    expect(events[1].percent).toBe(99)
  })

  it('emits a processing event when the body has fully landed', async () => {
    const events: UploadEvent[] = []
    const file = new File(['x'], 'clip.mp4', { type: 'video/mp4' })
    const p = uploadWithProgress(file, (e) => events.push(e))
    lastXhr!._sendProgress(100)
    lastXhr!._bodyFullySent()
    // Server is now transcoding — no response yet. The chip should
    // show "Processing video…" in this window.
    expect(events[events.length - 1].phase).toBe('processing')
    expect(events[events.length - 1].percent).toBe(100)
    lastXhr!._respondOk({ url: 'api/media/x.webm', filename: 'x.webm' })
    await p
  })

  it('updates the legacy global signal during uploading + processing', async () => {
    const file = new File(['x'], 'photo.jpg', { type: 'image/jpeg' })
    const p = uploadWithProgress(file)
    lastXhr!._sendProgress(50)
    expect(uploadProgress.value?.phase).toBe('uploading')
    expect(uploadProgress.value?.percent).toBe(50)
    lastXhr!._bodyFullySent()
    expect(uploadProgress.value?.phase).toBe('processing')
    expect(uploadProgress.value?.percent).toBe(100)
    lastXhr!._respondOk({ url: 'api/media/x.webp', filename: 'x.webp' })
    await p
    // Cleared on terminal.
    expect(uploadProgress.value).toBeNull()
  })

  it('rejects + emits a failed event on HTTP failure', async () => {
    const events: UploadEvent[] = []
    const file = new File(['x'], 'big.bin', { type: 'application/octet-stream' })
    const p = uploadWithProgress(file, (e) => events.push(e))
    lastXhr!._respondFail(413)
    await expect(p).rejects.toThrow(/413/)
    expect(events[events.length - 1].phase).toBe('failed')
  })

  it('rejects + emits a failed event on network error', async () => {
    const events: UploadEvent[] = []
    const file = new File(['x'], 'big.bin', { type: 'application/octet-stream' })
    const p = uploadWithProgress(file, (e) => events.push(e))
    lastXhr!._networkError()
    await expect(p).rejects.toThrow(/Upload failed/)
    expect(events[events.length - 1].phase).toBe('failed')
  })
})
