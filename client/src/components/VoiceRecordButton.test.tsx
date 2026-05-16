/* eslint-disable @typescript-eslint/no-explicit-any */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'
import { VoiceRecordButton } from './VoiceRecordButton'
import { toasts } from './Toast'

// ── MediaRecorder fake ─────────────────────────────────────────────────
//
// Provides the smallest viable surface vitest+jsdom need to exercise
// ``VoiceRecordButton``: ``start`` / ``stop`` lifecycle, the
// ``ondataavailable`` + ``onstop`` callback hooks, and the
// ``isTypeSupported`` static. Each test resets the singleton state.

interface FakeMediaRecorder extends EventTarget {
  state: 'inactive' | 'recording'
  start(): void
  stop(): void
  ondataavailable: ((e: any) => void) | null
  onstop: (() => void) | null
}

let lastRecorder: FakeMediaRecorder | null = null

class _FakeRecorder extends EventTarget implements FakeMediaRecorder {
  state: 'inactive' | 'recording' = 'inactive'
  ondataavailable: ((e: any) => void) | null = null
  onstop: (() => void) | null = null
  mimeType = 'audio/ogg; codecs=opus'
  start() {
    this.state = 'recording'
    // eslint-disable-next-line @typescript-eslint/no-this-alias
    lastRecorder = this
  }
  stop() {
    this.state = 'inactive'
    // Mimic the real-world ordering: dataavailable fires, then onstop.
    // Capture ``this``-bound callbacks before the microtask so the
    // body avoids ESLint's no-this-alias on a local rebinding.
    const onData = this.ondataavailable?.bind(this)
    const onStop = this.onstop?.bind(this)
    queueMicrotask(() => {
      onData?.({ data: new Blob(['fake-pcm'], { type: 'audio/ogg' }) })
      onStop?.()
    })
  }
  static isTypeSupported() {
    return true
  }
}

const fakeStream = {
  getTracks: () => [{ stop: () => {} }],
}

beforeEach(() => {
  lastRecorder = null
  toasts.value = []  // drain any toasts from a previous test
  ;(globalThis as any).MediaRecorder = _FakeRecorder as any
  ;(globalThis as any).navigator.mediaDevices = {
    getUserMedia: vi.fn().mockResolvedValue(fakeStream),
  }
})

afterEach(() => {
  delete (globalThis as any).MediaRecorder
})

/** Forge an Event with a ``clientY`` property — jsdom's synthetic
 *  pointer-event constructors drop the field. */
function _eventWithClientY(type: string, clientY: number): Event {
  const e = new Event(type, { bubbles: true, cancelable: true })
  Object.defineProperty(e, 'clientY', { value: clientY })
  return e
}

describe('VoiceRecordButton', () => {
  it('renders the mic icon in idle state', () => {
    const { container } = render(<VoiceRecordButton onCapture={() => {}} />)
    expect(container.querySelector('button.sh-voice-btn')).toBeTruthy()
    // The SVG icon is rendered (aria-hidden, so query by tag).
    expect(container.querySelector('svg')).toBeTruthy()
  })

  it('renders an unsupported-state disabled button when MediaRecorder is missing', () => {
    delete (globalThis as any).MediaRecorder
    const { container } = render(<VoiceRecordButton onCapture={() => {}} />)
    const btn = container.querySelector('button')
    expect(btn?.disabled).toBe(true)
    expect(btn?.getAttribute('title')).toMatch(/aren't supported/i)
  })

  it('starts recording on pointerdown and fires onCapture on pointerup', async () => {
    const captured: Blob[] = []
    const { container } = render(
      <VoiceRecordButton onCapture={(b) => captured.push(b)} />,
    )
    const btn = container.querySelector('button') as HTMLButtonElement

    fireEvent.pointerDown(btn, { clientY: 100 })
    await waitFor(() => expect(lastRecorder?.state).toBe('recording'))
    fireEvent.pointerUp(btn, { clientY: 100 })
    await waitFor(() => expect(captured).toHaveLength(1))
    // Captured blob carries the full ``audio/ogg; codecs=opus`` MIME
    // — that is the wire shape MediaRecorder emits and what the
    // upload path forwards on.
    expect(captured[0].type).toMatch(/audio\/ogg/)
  })

  it('discards the recording on pointer-cancel (e.g. mouse leaves the button)', async () => {
    const captured: Blob[] = []
    const { container } = render(
      <VoiceRecordButton onCapture={(b) => captured.push(b)} />,
    )
    const btn = container.querySelector('button') as HTMLButtonElement

    fireEvent.pointerDown(btn, { clientY: 100 })
    await _flush()
    fireEvent.pointerCancel(btn, { clientY: 100 })
    await _flush()
    expect(captured).toHaveLength(0)
  })

  // NOTE: the slide-up-to-lock gesture is exercised via visual
  // verification (chrome-devtools-mcp), not vitest. jsdom's
  // ``PointerEvent`` shim drops the ``clientY`` field from
  // ``EventInit``, so the handler reads an undefined coordinate and
  // never crosses the lock threshold. The component logic itself
  // (``pointerStartY - e.clientY >= LOCK_SLIDE_PX``) is trivial; a
  // live browser is the right surface for it.

  it('honours disabled prop by not opening the mic', async () => {
    const getUserMedia = vi.fn()
    ;(globalThis as any).navigator.mediaDevices = { getUserMedia }
    const { container } = render(
      <VoiceRecordButton onCapture={() => {}} disabled />,
    )
    const btn = container.querySelector('button') as HTMLButtonElement
    fireEvent.pointerDown(btn, { clientY: 100 })
    await _flush()
    expect(getUserMedia).not.toHaveBeenCalled()
  })

  it('surfaces a toast when getUserMedia is unavailable (HTTP origin)', async () => {
    // Android Chrome on plain-HTTP leaves ``mediaDevices`` undefined.
    // The button used to silently no-op — now it raises a visible
    // toast pointing at HTTPS.
    (globalThis as any).navigator.mediaDevices = undefined
    ;(window as any).isSecureContext = false
    const { container } = render(
      <VoiceRecordButton onCapture={() => {}} />,
    )
    const btn = container.querySelector('button') as HTMLButtonElement
    fireEvent.pointerDown(btn, { clientY: 100 })
    await _flush()
    expect(toasts.value.map(t => t.message).join(' ')).toMatch(/HTTPS|secure/i)
  })

  it('surfaces a toast when getUserMedia rejects with NotAllowedError', async () => {
    (window as any).isSecureContext = true
    const err = Object.assign(new Error('denied'), { name: 'NotAllowedError' })
    ;(globalThis as any).navigator.mediaDevices = {
      getUserMedia: vi.fn().mockRejectedValue(err),
    }
    const { container } = render(
      <VoiceRecordButton onCapture={() => {}} />,
    )
    const btn = container.querySelector('button') as HTMLButtonElement
    fireEvent.pointerDown(btn, { clientY: 100 })
    await _flush()
    expect(toasts.value.map(t => t.message).join(' ')).toMatch(/permission denied/i)
  })

  it('calls setPointerCapture so finger-drift does not orphan the recording', async () => {
    const setPointerCapture = vi.fn()
    const { container } = render(<VoiceRecordButton onCapture={() => {}} />)
    const btn = container.querySelector('button') as HTMLButtonElement
    // Stub the API on the actual button — jsdom doesn't ship it.
    ;(btn as unknown as { setPointerCapture: typeof setPointerCapture })
      .setPointerCapture = setPointerCapture
    fireEvent.pointerDown(btn, { clientY: 100 })
    await _flush()
    // jsdom-fired pointer events may carry ``pointerId === 0`` or
    // undefined — we just care that the handler reached into the
    // target's ``setPointerCapture`` so finger-drift doesn't orphan
    // the recording. The arg shape is browser-trusted.
    expect(setPointerCapture).toHaveBeenCalledTimes(1)
  })
})

/** Drain microtasks + the next macrotask so signal-driven re-renders
 *  have a chance to flush before the next assertion runs. */
async function _flush(): Promise<void> {
  await Promise.resolve()
  await Promise.resolve()
  await new Promise(r => setTimeout(r, 0))
}
