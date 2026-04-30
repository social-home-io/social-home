/**
 * CallsPage — active-calls tray (§26).
 *
 * Lists in-progress + ringing calls with one-tap "Return to call" /
 * "Hang up" actions. The incoming-call UX is now a separate global
 * overlay (:mod:`IncomingCallDialog`), and outbound calls are kicked
 * off from the DM thread's call buttons, so this page is just the
 * quick-switcher tray.
 */
import { useEffect } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import { signal, computed } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { active, type ActiveCall } from '@/store/calls'

const loading = signal(true)
const inProgress = computed(() => active.value.filter(c => c.status === 'in_progress'))
const ringingOut = computed(() => active.value.filter(c => c.status === 'ringing'))

export default function CallsPage() {
  useTitle('Calls')
  const loc = useLocation()

  useEffect(() => { void loadActiveCalls() }, [])

  if (loading.value) return <Spinner />

  const nothingActive =
    inProgress.value.length === 0 && ringingOut.value.length === 0

  return (
    <div class="sh-calls-page">
      <div class="sh-page-header">
      </div>

      {nothingActive && (
        <div class="sh-empty-state">
          <div style={{ fontSize: '2rem' }}>📞</div>
          <h3>No active calls</h3>
          <p>Start a call from a direct-message thread. Active + ringing
             calls will show up here so you can hop back to them.</p>
        </div>
      )}

      {inProgress.value.length > 0 && (
        <section class="sh-card" style={{ marginBottom: '1rem' }}>
          <h3 style={{ marginTop: 0 }}>In progress</h3>
          {inProgress.value.map(c => (
            <ActiveCallRow key={c.call_id} call={c}
                           onReturn={() => loc.route(`/calls/${c.call_id}`)} />
          ))}
        </section>
      )}

      {ringingOut.value.length > 0 && (
        <section class="sh-card">
          <h3 style={{ marginTop: 0 }}>Ringing out</h3>
          {ringingOut.value.map(c => (
            <ActiveCallRow key={c.call_id} call={c}
                           onReturn={() => loc.route(`/calls/${c.call_id}`)} />
          ))}
        </section>
      )}
    </div>
  )
}

function ActiveCallRow({ call, onReturn }: { call: ActiveCall, onReturn: () => void }) {
  return (
    <div class="sh-call-row sh-card">
      <span>{call.call_type === 'video' ? '📹' : '🔊'}</span>
      <span class="sh-call-peer">{call.caller} → {call.callee || '(group)'}</span>
      <span class="sh-call-status">{call.status}</span>
      <Button onClick={onReturn}>Return to call</Button>
      <Button onClick={() => hangUp(call.call_id)}>Hang up</Button>
    </div>
  )
}

async function loadActiveCalls() {
  loading.value = true
  try {
    active.value = await api.get('/api/calls/active') as ActiveCall[]
  } catch (err: unknown) {
    showToast(`Could not load calls: ${(err as Error)?.message ?? err}`, 'error')
    active.value = []
  } finally {
    loading.value = false
  }
}

async function hangUp(callId: string) {
  try {
    await api.post(`/api/calls/${callId}/hangup`, {})
  } catch (err: unknown) {
    showToast(`Hang up failed: ${(err as Error)?.message ?? err}`, 'error')
  }
  await loadActiveCalls()
}
