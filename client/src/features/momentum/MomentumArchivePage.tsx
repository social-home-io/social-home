/**
 * MomentumArchivePage — full retention-window list of moments
 * (§Momentum). Same API shape as the inbox but renders with date
 * headers so the calendar-style scroll works.
 */
import { useEffect } from 'preact/hooks'
import { signal, computed } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { useTitle } from '@/store/pageTitle'
import { ws } from '@/ws'
import type { Moment } from '@/types'

const moments = signal<Moment[]>([])
const loading = signal<boolean>(true)

const grouped = computed<Map<string, Moment[]>>(() => {
  const m = new Map<string, Moment[]>()
  for (const x of moments.value) {
    const day = x.created_at.slice(0, 10)
    if (!m.has(day)) m.set(day, [])
    m.get(day)!.push(x)
  }
  return m
})


export default function MomentumArchivePage() {
  useTitle('Moments archive')
  const loc = useLocation()

  useEffect(() => {
    loading.value = true
    const fetchAll = (initial: boolean) =>
      api.get('/api/moments/archive')
        .then((rows: Moment[]) => {
          moments.value = rows ?? []
          if (initial) loading.value = false
        })
        .catch((err: unknown) => {
          if (initial) loading.value = false
          showToast(`Failed to load archive: ${(err as Error)?.message ?? err}`,
            'error')
        })
    void fetchAll(true)
    const dispose = [
      ws.on('moment.created', () => { void fetchAll(false) }),
      ws.on('moment.deleted', () => { void fetchAll(false) }),
    ]
    return () => { dispose.forEach(d => d()) }
  }, [])

  if (loading.value) return <Spinner />

  const days = [...grouped.value.keys()]  // already newest-first
  if (days.length === 0) {
    return (
      <div class="sh-momentum-archive">
        <h2>Moments archive</h2>
        <div class="sh-empty-state">
          <p>No moments in the retention window yet.</p>
        </div>
      </div>
    )
  }

  return (
    <div class="sh-momentum-archive">
      <h2>Moments archive</h2>
      <p class="sh-muted">
        Moments live 24 h by default; 7 d for people you follow.
      </p>
      {days.map(day => (
        <section key={day} class="sh-momentum-archive-day">
          <h3>{new Date(day + 'T00:00:00Z').toLocaleDateString(undefined, {
            weekday: 'long', year: 'numeric', month: 'long', day: 'numeric',
          })}</h3>
          <ul class="sh-momentum-list">
            {grouped.value.get(day)!.map(m => (
              <li key={m.id} class="sh-momentum-row">
                <a href={`/momentum/${m.id}`}
                  class="sh-momentum-row-link"
                  onClick={(ev) => {
                    ev.preventDefault()
                    loc.route(`/momentum/${m.id}`)
                  }}>
                  <Avatar name={m.author_user_id} size={32} />
                  <div class="sh-momentum-row-body">
                    <div class="sh-momentum-row-meta">
                      <strong>{m.author_user_id}</strong>
                      <span class="sh-muted">{m.created_at.slice(11, 16)}</span>
                    </div>
                    {m.content && (
                      <p class="sh-momentum-row-content">{m.content}</p>
                    )}
                    {m.media_type === 'image' && m.media_url && (
                      <img src={m.media_url} alt="" loading="lazy"
                        class="sh-momentum-row-media" />
                    )}
                    {m.media_type === 'video' && m.media_url && (
                      <span class="sh-momentum-row-media sh-momentum-row-media--video">
                        🎬 video
                      </span>
                    )}
                  </div>
                </a>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  )
}
