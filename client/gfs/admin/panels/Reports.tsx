import { useCallback, useEffect, useState } from 'preact/hooks'
import { api, fmtTime } from '../api'

interface FraudReport {
  id: string
  target_type: string
  target_id: string
  category: string
  notes?: string | null
  reporter_instance_id: string
  created_at: number
}

export function ReportsPanel() {
  const [list, setList] = useState<FraudReport[]>([])
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    try {
      const data = await api<FraudReport[]>(
        'GET', '/admin/api/reports?status=pending',
      )
      setList(data)
      setErr(null)
    } catch (e) {
      setErr((e as Error).message)
    }
  }, [])

  useEffect(() => { void reload() }, [reload])

  const action = async (id: string, name: string) => {
    await api('POST', `/admin/api/reports/${encodeURIComponent(id)}/review`,
      { action: name })
    await reload()
  }

  return (
    <>
      <h2>Fraud reports</h2>
      {err && <p class="error">{err}</p>}
      <table>
        <thead>
          <tr>
            <th>Target</th>
            <th>Category</th>
            <th>Reporter</th>
            <th>Created</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {list.length === 0 && (
            <tr><td colSpan={5} class="muted">No pending reports.</td></tr>
          )}
          {list.map((r) => (
            <tr key={r.id}>
              <td>
                <strong>{r.target_type}</strong> {r.target_id}
                {r.notes && <div class="muted">{r.notes}</div>}
              </td>
              <td>{r.category}</td>
              <td style={{ fontFamily: 'monospace' }}>
                {r.reporter_instance_id}
              </td>
              <td><time>{fmtTime(r.created_at)}</time></td>
              <td class="row-actions">
                <button class="secondary" onClick={() => void action(r.id, 'dismiss')}>
                  Dismiss
                </button>
                <button class="danger" onClick={() => void action(r.id, 'ban_target')}>
                  Ban target
                </button>
                {r.target_type === 'space' && (
                  <button class="danger" onClick={() => void action(r.id, 'ban_instance')}>
                    Ban instance
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
