/**
 * CallTypePickerDialog — "audio or video?" picker for outbound calls (§26.2).
 *
 * Mounted at the app root so any thread-header / call-back button can
 * surface the picker without owning its own dialog. Follows the
 * shared-Modal + signal-driven open pattern used by ``StickyDialog``,
 * ``CalendarEventDialog``, ``NewDmDialog``, etc., so the washi-tape
 * chrome / focus trap / Escape close are consistent across the app.
 *
 * Flow:
 *   1. ``openCallTypePicker(conversationId)`` flips the open signal.
 *   2. User picks Audio or Video — the dialog POSTs ``/api/calls`` with
 *      the chosen ``call_type`` and routes to the in-call page.
 *   3. The chosen ``call_type`` is fixed at offer time on the backend
 *      (spec §26.5); mid-call camera enable/disable is handled by
 *      :func:`InCallPage.toggleCamera`.
 */
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { Modal } from './Modal'
import { showToast } from './Toast'

const open = signal(false)
const conversationId = signal<string | null>(null)
const submitting = signal(false)

export function openCallTypePicker(convId: string): void {
  conversationId.value = convId
  submitting.value = false
  open.value = true
}

export function CallTypePickerDialog() {
  const loc = useLocation()

  const start = async (callType: 'audio' | 'video') => {
    const convId = conversationId.value
    if (!convId || submitting.value) return
    submitting.value = true
    try {
      const r = await api.post('/api/calls', {
        conversation_id: convId,
        call_type: callType,
        sdp_offer: 'v=0\r\n',
      }) as { call_id: string }
      open.value = false
      loc.route(`/calls/${r.call_id}`)
    } catch (err: unknown) {
      showToast(`Call failed: ${(err as Error)?.message ?? err}`, 'error')
      submitting.value = false
    }
  }

  if (!open.value) return null
  return (
    <Modal
      open={open.value}
      onClose={() => { open.value = false }}
      title="Start a call"
    >
      <div class="sh-call-picker" role="group" aria-label="Choose call type">
        <button
          type="button"
          class="sh-call-picker-tile"
          onClick={() => void start('audio')}
          disabled={submitting.value}
          aria-label="Start audio call"
        >
          <span class="sh-call-picker-icon" aria-hidden="true">📞</span>
          <span class="sh-call-picker-label">Audio</span>
          <span class="sh-call-picker-meta">Voice only — turn on the camera later if you want.</span>
        </button>
        <button
          type="button"
          class="sh-call-picker-tile"
          onClick={() => void start('video')}
          disabled={submitting.value}
          aria-label="Start video call"
        >
          <span class="sh-call-picker-icon" aria-hidden="true">📹</span>
          <span class="sh-call-picker-label">Video</span>
          <span class="sh-call-picker-meta">Camera on from the start.</span>
        </button>
      </div>
    </Modal>
  )
}
