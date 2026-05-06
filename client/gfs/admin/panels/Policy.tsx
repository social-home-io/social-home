import { useCallback, useEffect, useState } from 'preact/hooks'
import { api } from '../api'

interface Policy {
  auto_accept_clients: boolean
  auto_accept_spaces: boolean
  fraud_threshold: number
}

export function PolicyPanel() {
  const [policy, setPolicy] = useState<Policy | null>(null)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    try {
      const data = await api<Policy>('GET', '/admin/api/policy')
      setPolicy(data)
      setErr(null)
    } catch (e) {
      setErr((e as Error).message)
    }
  }, [])

  useEffect(() => { void reload() }, [reload])

  const save = async () => {
    if (!policy) return
    setSaving(true)
    try {
      await api('PATCH', '/admin/api/policy', policy)
      await reload()
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  if (err) return <p class="error">{err}</p>
  if (!policy) return <p class="muted">Loading…</p>
  const update = <K extends keyof Policy>(key: K, value: Policy[K]) =>
    setPolicy({ ...policy, [key]: value })

  return (
    <>
      <h2>Policy</h2>
      <div class="field">
        <label>
          <input
            type="checkbox"
            checked={policy.auto_accept_clients}
            onChange={(e) => update('auto_accept_clients', (e.currentTarget as HTMLInputElement).checked)}
          /> Auto-accept new clients
        </label>
      </div>
      <div class="field">
        <label>
          <input
            type="checkbox"
            checked={policy.auto_accept_spaces}
            onChange={(e) => update('auto_accept_spaces', (e.currentTarget as HTMLInputElement).checked)}
          /> Auto-accept new global spaces
        </label>
      </div>
      <div class="field">
        <label>
          Fraud threshold (distinct reporters before auto-ban)
          <input
            type="number"
            min={1}
            value={policy.fraud_threshold}
            onInput={(e) => update('fraud_threshold',
              parseInt((e.currentTarget as HTMLInputElement).value, 10) || 1)}
          />
        </label>
      </div>
      <button class="primary" onClick={() => void save()} disabled={saving}>
        {saving ? 'Saving…' : 'Save'}
      </button>
    </>
  )
}
