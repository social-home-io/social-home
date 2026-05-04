import { signal } from '@preact/signals'

interface ToastItem {
  id: number
  message: string
  type: 'info' | 'success' | 'error'
  /** How many identical (message+type) toasts have been collapsed
   *  into this row. Rendered as "× N" when > 1. */
  count: number
  /** Active auto-dismiss timer id — kept on the row so a duplicate
   *  call can clear it before scheduling a fresh one. */
  timeoutId: ReturnType<typeof setTimeout>
}

/** Cap on the number of toast rows visible at once. WS bursts (a
 *  flurry of ``post.created`` while the user scrolls a busy feed)
 *  could otherwise stack a tall queue the user has to wait through. */
const MAX_VISIBLE = 3

/** How long a single toast stays on screen before auto-dismissal. */
const DISMISS_AFTER_MS = 4000

let nextId = 0
export const toasts = signal<ToastItem[]>([])

function dropById(id: number) {
  toasts.value = toasts.value.filter(t => t.id !== id)
}

function scheduleDismiss(id: number): ReturnType<typeof setTimeout> {
  return setTimeout(() => dropById(id), DISMISS_AFTER_MS)
}

export function showToast(message: string, type: ToastItem['type'] = 'info') {
  // Dedupe: if the same (message, type) is already on screen, bump
  // its count and reset the timer instead of pushing a duplicate row.
  // Slack / WhatsApp do the same — three "Sent" toasts in a row read
  // as noise, "Sent × 3" reads as a count.
  const existing = toasts.value.find(
    t => t.message === message && t.type === type,
  )
  if (existing) {
    clearTimeout(existing.timeoutId)
    const timeoutId = scheduleDismiss(existing.id)
    toasts.value = toasts.value.map(t =>
      t.id === existing.id
        ? { ...t, count: t.count + 1, timeoutId }
        : t,
    )
    return
  }

  const id = nextId++
  const timeoutId = scheduleDismiss(id)
  const next = [...toasts.value, { id, message, type, count: 1, timeoutId }]
  // Cap visible rows. When overflowing, evict the oldest entries —
  // their auto-dismiss timers stay valid (they no-op on missing ids).
  while (next.length > MAX_VISIBLE) {
    const dropped = next.shift()
    if (dropped) clearTimeout(dropped.timeoutId)
  }
  toasts.value = next
}

export function ToastContainer() {
  return (
    <div class="sh-toast-container" role="region" aria-live="polite">
      {toasts.value.map(t => (
        <div key={t.id} class={`sh-toast sh-toast--${t.type}`}>
          <span class="sh-toast-message">{t.message}</span>
          {t.count > 1 && (
            <span class="sh-toast-count" aria-label={`${t.count} occurrences`}>
              × {t.count}
            </span>
          )}
        </div>
      ))}
    </div>
  )
}
