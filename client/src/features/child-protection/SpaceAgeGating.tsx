/**
 * SpaceAgeGating — space-level age restrictions (spec §23.105).
 *
 * Household admins set a minimum age + target-audience label on a
 * space. Minors whose ``declared_age`` is below ``min_age`` are blocked
 * from joining (backend enforcement in ``SpaceService.add_member``
 * via ``ChildProtectionService.check_space_age_gate``).
 *
 * Listens for ``cp.age_gate_changed`` so a change on another session
 * refreshes this one live.
 */
import { useEffect } from 'preact/hooks'
import { useSignal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'

type Audience = 'all' | 'family' | 'teen' | 'adult'

const VALID_MIN_AGES: number[] = [0, 13, 16, 18]
const AUDIENCES: { value: Audience; label: string }[] = [
  { value: 'all',    label: 'All'    },
  { value: 'family', label: 'Family' },
  { value: 'teen',   label: 'Teen'   },
  { value: 'adult',  label: 'Adult'  },
]

interface Gate {
  min_age: number
  target_audience: Audience
}

export function SpaceAgeGating({ spaceId }: { spaceId: string }) {
  // ``useSignal`` so writes from ``load()`` and ``save()`` land in the
  // same signal instance the JSX is subscribed to across re-renders.
  const gate = useSignal<Gate>({ min_age: 0, target_audience: 'all' })
  const saving = useSignal(false)
  const loaded = useSignal(false)

  const load = async () => {
    try {
      const data = await api.get(
        `/api/cp/spaces/${spaceId}/age-gate`,
      ) as Gate
      gate.value = {
        min_age: Number(data.min_age) || 0,
        target_audience: (data.target_audience as Audience) || 'all',
      }
    } catch {
      /* unknown space or unauthenticated — leave defaults */
    } finally {
      loaded.value = true
    }
  }

  useEffect(() => {
    void load()
    const off = ws.on('cp.age_gate_changed', (evt) => {
      const d = evt.data as { space_id?: string }
      if (d.space_id === spaceId) void load()
    })
    return () => { off() }
  }, [spaceId])

  const save = async () => {
    saving.value = true
    try {
      await api.patch(`/api/cp/spaces/${spaceId}/age-gate`, {
        min_age: gate.value.min_age,
        target_audience: gate.value.target_audience,
      })
      showToast('Age gate saved', 'success')
    } catch (e: unknown) {
      showToast((e as Error).message || 'Save failed', 'error')
    } finally {
      saving.value = false
    }
  }

  if (!loaded.value) return null

  return (
    <div class="sh-age-gating sh-card">
      <h4>Age gate</h4>
      <p class="sh-muted">
        Children with child protection enabled are blocked from joining
        this space when their age is below the minimum you set here. The
        audience label is a hint for how the space appears in public
        discovery — it doesn't block anyone on its own.
      </p>
      <div class="sh-form-row">
        <label>
          Minimum age
          <select
            value={String(gate.value.min_age)}
            onChange={(e) => gate.value = {
              ...gate.value,
              min_age: Number((e.target as HTMLSelectElement).value),
            }}>
            {VALID_MIN_AGES.map(a => (
              <option key={a} value={String(a)}>
                {a === 0 ? 'No restriction' : `${a}+`}
              </option>
            ))}
          </select>
        </label>
        <label>
          Audience
          <select
            value={gate.value.target_audience}
            onChange={(e) => gate.value = {
              ...gate.value,
              target_audience: (e.target as HTMLSelectElement).value as Audience,
            }}>
            {AUDIENCES.map(a => (
              <option key={a.value} value={a.value}>{a.label}</option>
            ))}
          </select>
        </label>
        <Button onClick={save} loading={saving.value}>Save</Button>
      </div>
    </div>
  )
}
