import { useEffect, useState } from 'preact/hooks'
import { api, fmtTime } from '../api'

interface AuditRow {
  action: string
  target_type?: string | null
  target_id?: string | null
  admin_ip?: string | null
  created_at: number
}

export function AuditPanel() {
  const [rows, setRows] = useState<AuditRow[]>([])
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    api<AuditRow[]>('GET', '/admin/api/audit?limit=200')
      .then(setRows)
      .catch((e) => setErr((e as Error).message))
  }, [])
  if (err) return <p class="error">{err}</p>
  return (
    <>
      <h2>Admin audit log</h2>
      {rows.length === 0 ? (
        <div class="muted">No audit entries yet.</div>
      ) : (
        rows.map((r, i) => (
          <div key={i} class="audit-row">
            <strong>{r.action}</strong>
            {r.target_type && (
              <> · <em>{r.target_type}</em> {r.target_id}</>
            )}
            <br />
            <time>{fmtTime(r.created_at)}</time>
            {' '}· from {r.admin_ip || 'unknown'}
          </div>
        ))
      )}
    </>
  )
}
