/**
 * CallsTab — active-call tray rendered inside the Chats panel.
 *
 * Replaces the standalone ``CallsPage``. Behaviour is identical
 * (lists in-progress + ringing-out calls, "Return to call" /
 * "Hang up" actions); the global :func:`wireCallsWs` already keeps
 * the ``active`` signal fresh, so this component stays a pure
 * consumer.
 *
 * The single per-tab side effect is the initial
 * ``/api/calls/active`` fetch on mount, in case the user opened the
 * Chats panel after a call started elsewhere.
 */
import { useEffect } from 'preact/hooks'
import { signal, computed } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { active, type ActiveCall } from '@/store/calls'

const loading = signal(true)
const inProgress = computed(() =>
  active.value.filter((c) => c.status === 'in_progress'),
)
const ringingOut = computed(() =>
  active.value.filter((c) => c.status === 'ringing'),
)

export default function CallsTab() {
  const loc = useLocation()
  useEffect(() => { void loadActiveCalls() }, [])

  if (loading.value) return <Spinner />

  const nothingActive =
    inProgress.value.length === 0 && ringingOut.value.length === 0

  return (
    <div class="sh-calls-page">
      {nothingActive && (
        <div class="sh-empty-state">
          <div aria-hidden="true">📞</div>
          <h3>No active calls</h3>
          <p>
            Start a call from a direct-message thread. Active + ringing
            calls show up here so you can hop back to them.
          </p>
        </div>
      )}

      {inProgress.value.length > 0 && (
        <section class="sh-card" style={{ marginBottom: '1rem' }}>
          <h3 style={{ marginTop: 0 }}>In progress</h3>
          {inProgress.value.map((c) => (
            <ActiveCallRow
              key={c.call_id}
              call={c}
              onReturn={() => loc.route(`/calls/${c.call_id}`)}
            />
          ))}
        </section>
      )}

      {ringingOut.value.length > 0 && (
        <section class="sh-card">
          <h3 style={{ marginTop: 0 }}>Ringing out</h3>
          {ringingOut.value.map((c) => (
            <ActiveCallRow
              key={c.call_id}
              call={c}
              onReturn={() => loc.route(`/calls/${c.call_id}`)}
            />
          ))}
        </section>
      )}
    </div>
  )
}

function ActiveCallRow({
  call,
  onReturn,
}: {
  call: ActiveCall
  onReturn: () => void
}) {
  return (
    <div class="sh-call-row sh-card">
      <span>{call.call_type === 'video' ? '📹' : '🔊'}</span>
      <span class="sh-call-peer">
        {call.caller} → {call.callee || '(group)'}
      </span>
      <span class="sh-call-status">{call.status}</span>
      <Button onClick={onReturn}>Return to call</Button>
      <Button onClick={() => hangUp(call.call_id)}>Hang up</Button>
    </div>
  )
}

async function loadActiveCalls() {
  loading.value = true
  try {
    active.value = (await api.get('/api/calls/active')) as ActiveCall[]
  } catch (err: unknown) {
    showToast(
      `Could not load calls: ${(err as Error)?.message ?? err}`,
      'error',
    )
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
