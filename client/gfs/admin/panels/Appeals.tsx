import { useCallback, useEffect, useState } from 'preact/hooks'
import { api, fmtTime } from '../api'

interface Appeal {
  id: string
  target_type: string
  target_id: string
  message: string | null
  created_at: number
}

export function AppealsPanel() {
  const [list, setList] = useState<Appeal[]>([])
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    try {
      const data = await api<Appeal[]>('GET', '/admin/api/appeals?status=pending')
      setList(data)
      setErr(null)
    } catch (e) {
      setErr((e as Error).message)
    }
  }, [])

  useEffect(() => { void reload() }, [reload])

  const action = async (id: string, name: string) => {
    await api('POST', `/admin/api/appeals/${encodeURIComponent(id)}/decide`,
      { action: name })
    await reload()
  }

  return (
    <>
      <h2>Appeals</h2>
      {err && <p class="error">{err}</p>}
      <table>
        <thead>
          <tr>
            <th>Target</th>
            <th>Message</th>
            <th>Filed</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {list.length === 0 && (
            <tr><td colSpan={4} class="muted">No pending appeals.</td></tr>
          )}
          {list.map((a) => (
            <tr key={a.id}>
              <td><strong>{a.target_type}</strong> {a.target_id}</td>
              <td>
                {a.message ? a.message : <span class="muted">(no message)</span>}
              </td>
              <td><time>{fmtTime(a.created_at)}</time></td>
              <td class="row-actions">
                <button class="primary" onClick={() => void action(a.id, 'lift')}>
                  Lift ban
                </button>
                <button class="secondary" onClick={() => void action(a.id, 'dismiss')}>
                  Dismiss
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
