/**
 * MomentumPage — inbox for the Momentum pillar (§Momentum).
 *
 * Renders the chronological list of moments visible to the viewer
 * (server-side filter: 24h base retention, 7d for moments authored by
 * users the viewer follows, blocked authors hidden). Live updates land
 * via the `moment.*` WS frames and trigger a refetch.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { openUserActions } from '@/components/UserActionsMenu'
import { blockedUserIds, loadBlocks } from '@/store/blocks'
import { currentUser } from '@/store/auth'
import { useTitle } from '@/store/pageTitle'
import { ws } from '@/ws'
import type { Moment } from '@/types'

const moments = signal<Moment[]>([])
const loading = signal<boolean>(true)


function relativeTime(iso: string): string {
  const dt = new Date(iso)
  if (Number.isNaN(dt.getTime())) return iso
  const ms = Date.now() - dt.getTime()
  const m = Math.floor(ms / 60_000)
  if (m < 1)   return 'just now'
  if (m < 60)  return `${m} min ago`
  const h = Math.floor(m / 60)
  if (h < 24)  return `${h} h ago`
  const d = Math.floor(h / 24)
  if (d < 7)   return `${d} d ago`
  return dt.toLocaleDateString()
}


export default function MomentumPage() {
  useTitle('Momentum')
  const loc = useLocation()
  const me = currentUser.value?.user_id

  useEffect(() => {
    loading.value = true
    void loadBlocks()
    const fetchInbox = (initial: boolean) =>
      api.get('/api/moments')
        .then((rows: Moment[]) => {
          moments.value = rows ?? []
          if (initial) loading.value = false
        })
        .catch((err: unknown) => {
          if (initial) loading.value = false
          showToast(`Failed to load moments: ${(err as Error)?.message ?? err}`,
            'error')
        })
    void fetchInbox(true)
    const dispose = [
      ws.on('moment.created',          () => { void fetchInbox(false) }),
      ws.on('moment.deleted',          () => { void fetchInbox(false) }),
      ws.on('moment.reaction_changed', () => { void fetchInbox(false) }),
    ]
    return () => { dispose.forEach(d => d()) }
  }, [])

  if (loading.value) return <Spinner />

  // Defence-in-depth: server already strips blocked authors. Filter
  // again locally so a same-tab "Block @bob" hides them without a
  // refresh — the next fetch confirms.
  const blocked = blockedUserIds.value
  const visible = moments.value.filter(m => !blocked.has(m.author_user_id))
  // Inbox shows top-level moments only — replies open in detail view.
  const topLevel = visible.filter(m => !m.parent_moment_id)

  return (
    <div class="sh-momentum">
      <header class="sh-momentum-header">
        <h2>Momentum</h2>
        <a href="/momentum/archive" class="sh-link sh-momentum-archive-link">
          📅 Archive
        </a>
        <Button onClick={() => loc.route('/momentum/new')}>+ New moment</Button>
      </header>

      {topLevel.length === 0 && (
        <div class="sh-empty-state">
          <div style={{ fontSize: '2rem' }} aria-hidden="true">🌅</div>
          <h3 style={{ margin: 0 }}>No moments yet</h3>
          <p>
            A moment is a one-shot post that fans out to your paired
            households and theirs. They live 24 h by default, or 7 d
            for people you follow.
          </p>
          <div style={{ marginTop: '0.75rem' }}>
            <Button onClick={() => loc.route('/momentum/new')}>
              Share your first moment
            </Button>
          </div>
        </div>
      )}

      <ul class="sh-momentum-list" aria-label="Moments">
        {topLevel.map(m => (
          <li key={m.id} class="sh-momentum-row">
            <a href={`/momentum/${m.id}`} class="sh-momentum-row-link">
              <Avatar name={m.author_user_id} size={40} />
              <div class="sh-momentum-row-body">
                <div class="sh-momentum-row-meta">
                  <strong>
                    {m.author_user_id === me ? 'You' : m.author_user_id}
                  </strong>
                  <span class="sh-muted">{relativeTime(m.created_at)}</span>
                </div>
                {m.content && (
                  <p class="sh-momentum-row-content">{m.content}</p>
                )}
                {m.media_type === 'image' && m.media_url && (
                  <img src={m.media_url} alt="" loading="lazy"
                    class="sh-momentum-row-media" />
                )}
                {m.media_type === 'video' && m.media_url && (
                  <video src={m.media_url} controls muted preload="metadata"
                    class="sh-momentum-row-media" />
                )}
              </div>
            </a>
            {m.author_user_id !== me && (
              <button
                type="button"
                class="sh-momentum-row-overflow"
                aria-label={`More actions for ${m.author_user_id}`}
                onClick={(ev) => {
                  ev.preventDefault()
                  openUserActions(m.author_user_id)
                }}
              >
                ⋯
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
