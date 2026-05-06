import { useCallback, useEffect, useState } from 'preact/hooks'
import { api, pillClass } from '../api'

interface Space {
  space_id: string
  name: string
  owning_instance: string
  subscriber_count: number
  status: string
}

const FILTERS: Array<{ key: string; label: string }> = [
  { key: '',        label: 'All' },
  { key: 'active',  label: 'Active' },
  { key: 'pending', label: 'Pending' },
  { key: 'banned',  label: 'Banned' },
]

export function SpacesPanel() {
  const [filter, setFilter] = useState('')
  const [list, setList] = useState<Space[]>([])
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    try {
      const qs = filter ? `?status=${encodeURIComponent(filter)}` : ''
      const data = await api<Space[]>('GET', `/admin/api/spaces${qs}`)
      setList(data)
      setErr(null)
    } catch (e) {
      setErr((e as Error).message)
    }
  }, [filter])

  useEffect(() => { void reload() }, [reload])

  const action = async (id: string, name: string) => {
    await api('POST', `/admin/api/spaces/${encodeURIComponent(id)}/${name}`)
    await reload()
  }

  return (
    <>
      <h2>Spaces</h2>
      <div style={{ marginBottom: '10px' }}>
        {FILTERS.map((f) => (
          <button
            key={f.key}
            class={`secondary${filter === f.key ? ' is-active' : ''}`}
            onClick={() => setFilter(f.key)}
          >
            {f.label}
          </button>
        ))}
      </div>
      {err && <p class="error">{err}</p>}
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Owner</th>
            <th>Subs</th>
            <th>Status</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {list.length === 0 && (
            <tr><td colSpan={5} class="muted">No spaces.</td></tr>
          )}
          {list.map((s) => (
            <tr key={s.space_id}>
              <td>
                {s.name || '—'}
                <div class="muted">{s.space_id}</div>
              </td>
              <td style={{ fontFamily: 'monospace' }}>{s.owning_instance}</td>
              <td>{s.subscriber_count}</td>
              <td><span class={pillClass(s.status)}>{s.status}</span></td>
              <td class="row-actions">
                {s.status === 'pending' && (
                  <>
                    <button class="primary" onClick={() => void action(s.space_id, 'accept')}>
                      Accept
                    </button>
                    <button class="secondary" onClick={() => void action(s.space_id, 'reject')}>
                      Reject
                    </button>
                  </>
                )}
                {s.status === 'active' && (
                  <button class="danger" onClick={() => void action(s.space_id, 'ban')}>
                    Ban
                  </button>
                )}
                {s.status === 'banned' && (
                  <button class="secondary" onClick={() => void action(s.space_id, 'unban')}>
                    Unban
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
