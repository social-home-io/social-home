/**
 * RetentionConfig — space retention exemption settings (§23.132).
 */
import { useSignal } from '@preact/signals'
import { api } from '@/api'
import { Button } from './Button'
import { showToast } from './Toast'

const EXEMPT_TYPES = ['pages', 'gallery', 'tasks', 'calendar', 'stickies']

export function RetentionConfig({ spaceId, retentionDays, exemptTypes, onSave }: {
  spaceId: string; retentionDays: number | null
  exemptTypes: string[]; onSave?: () => void
}) {
  const days = useSignal(retentionDays?.toString() || '')
  const exempt = useSignal(new Set(exemptTypes))
  const saving = useSignal(false)

  const toggleExempt = (type: string) => {
    const s = new Set(exempt.value)
    if (s.has(type)) s.delete(type); else s.add(type)
    exempt.value = s
  }

  const save = async () => {
    saving.value = true
    try {
      await api.patch(`/api/spaces/${spaceId}`, {
        retention_days: days.value ? parseInt(days.value) : null,
        retention_exempt_types: Array.from(exempt.value),
      })
      showToast('Retention settings saved', 'success')
      onSave?.()
    } catch (e: any) { showToast(e.message || 'Failed', 'error') }
    finally { saving.value = false }
  }

  return (
    <div class="sh-retention">
      <h4>Content Retention</h4>
      <label>
        Auto-delete posts older than (days)
        <input type="number" min={0} value={days.value} placeholder="No limit"
          onInput={(e) => days.value = (e.target as HTMLInputElement).value} />
      </label>
      <p class="sh-muted">Leave empty for no automatic deletion.</p>

      {days.value && parseInt(days.value) > 0 && (
        <>
          <h5>Exempt from deletion:</h5>
          {EXEMPT_TYPES.map(type => (
            <label key={type} class="sh-toggle-row">
              <input type="checkbox" checked={exempt.value.has(type)}
                onChange={() => toggleExempt(type)} />
              {type.charAt(0).toUpperCase() + type.slice(1)}
            </label>
          ))}
        </>
      )}
      <Button onClick={save} loading={saving.value}>Save</Button>
    </div>
  )
}
