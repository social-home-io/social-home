/**
 * IncomingCallDialog — full-screen ringing overlay (spec §26.2).
 *
 * Mounted at the app root so a ringing call surfaces from *any* page.
 * Watches :data:`incoming` from ``@/store/calls``. Plays a short ringtone
 * (synthesised via the Web Audio API so we don't need to ship a binary
 * asset) + triggers a vibration pattern on mobile. Auto-dismisses after
 * 90 s (server-side TTL), falling back to a "You missed a call" toast.
 *
 * Keyboard:
 *
 * * ``Enter``  — accept the call
 * * ``Esc``    — decline
 */
import { useEffect, useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { incoming } from '@/store/calls'

const RING_TTL_MS = 90_000

let _ringStop: (() => void) | null = null

function startRingtone(): () => void {
  // Synthesise a soft two-note ring using WebAudio. Returns a stop fn.
  try {
    const Ctx = (globalThis as { AudioContext?: typeof AudioContext }).AudioContext
    if (!Ctx) return () => {}
    const ctx = new Ctx()
    const osc = ctx.createOscillator()
    const gain = ctx.createGain()
    osc.connect(gain); gain.connect(ctx.destination)
    osc.type = 'sine'; osc.frequency.value = 440
    gain.gain.value = 0
    const now = ctx.currentTime
    // Two-note cadence repeating every 2 s for 90 s.
    for (let t = 0; t < RING_TTL_MS / 1000; t += 2) {
      gain.gain.setValueAtTime(0.0, now + t)
      gain.gain.linearRampToValueAtTime(0.12, now + t + 0.05)
      gain.gain.linearRampToValueAtTime(0.0, now + t + 0.45)
      osc.frequency.setValueAtTime(440, now + t + 0.0)
      osc.frequency.setValueAtTime(554, now + t + 0.5)
      gain.gain.setValueAtTime(0.0, now + t + 0.5)
      gain.gain.linearRampToValueAtTime(0.12, now + t + 0.55)
      gain.gain.linearRampToValueAtTime(0.0, now + t + 0.95)
    }
    osc.start(now)
    osc.stop(now + RING_TTL_MS / 1000)
    return () => { try { osc.stop() } catch { /* noop */ } ctx.close() }
  } catch {
    return () => {}
  }
}

function startVibration(): () => void {
  if (typeof navigator === 'undefined' || !navigator.vibrate) return () => {}
  const pattern = [400, 200, 400, 200, 400]
  let stopped = false
  const tick = () => {
    if (stopped) return
    navigator.vibrate(pattern)
    setTimeout(tick, 2000)
  }
  tick()
  return () => { stopped = true; try { navigator.vibrate(0) } catch { /* noop */ } }
}

export default function IncomingCallDialog() {
  const loc = useLocation()
  // Both accept and decline fire one POST. The dialog stays mounted
  // while the request flies; the spinner says "we heard your tap".
  const [accepting, setAccepting] = useState(false)
  const [declining, setDeclining] = useState(false)

  useEffect(() => {
    if (!incoming.value) return
    _ringStop = (() => {
      const stopSnd = startRingtone()
      const stopVib = startVibration()
      return () => { stopSnd(); stopVib() }
    })()

    const timer = setTimeout(() => {
      if (incoming.value) {
        showToast(`You missed a call from ${incoming.value.from_user}`, 'info')
        incoming.value = null
      }
    }, RING_TTL_MS)

    const onKey = (e: KeyboardEvent) => {
      if (!incoming.value) return
      if (e.key === 'Enter') accept()
      if (e.key === 'Escape') decline()
    }
    window.addEventListener('keydown', onKey)

    return () => {
      clearTimeout(timer)
      window.removeEventListener('keydown', onKey)
      if (_ringStop) { _ringStop(); _ringStop = null }
    }
  }, [incoming.value?.call_id])

  if (!incoming.value) return null
  const call = incoming.value

  const accept = async () => {
    if (accepting || declining) return
    setAccepting(true)
    try {
      await api.post(`/api/calls/${call.call_id}/answer`, {
        sdp_answer: 'v=0\r\n',
      })
      incoming.value = null
      loc.route(`/calls/${call.call_id}`)
    } catch (err) {
      showToast(`Accept failed: ${(err as Error).message ?? err}`, 'error')
      setAccepting(false)
    }
  }
  const decline = async () => {
    if (accepting || declining) return
    setDeclining(true)
    try {
      await api.post(`/api/calls/${call.call_id}/decline`, {})
    } catch { /* best-effort */ }
    incoming.value = null
  }

  return (
    <div class="sh-incoming-overlay" role="dialog" aria-label="Incoming call">
      <div class="sh-incoming-card">
        <div class="sh-incoming-avatar" aria-hidden="true">
          {call.call_type === 'video' ? '📹' : '📞'}
        </div>
        <strong class="sh-incoming-name">{call.from_user} is calling</strong>
        <span class="sh-incoming-type">{call.call_type === 'video' ? 'Video call' : 'Audio call'}</span>
        <div class="sh-incoming-actions">
          <Button class="sh-accept" onClick={accept}
                  loading={accepting} disabled={declining}>Accept</Button>
          <Button class="sh-decline" onClick={decline}
                  loading={declining} disabled={accepting}>Decline</Button>
        </div>
      </div>
    </div>
  )
}
