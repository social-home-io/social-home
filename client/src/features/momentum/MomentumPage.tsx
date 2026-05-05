/**
 * MomentumPage — inbox for the Momentum pillar (§Momentum).
 *
 * Twitter-style row layout: tight rows with an inline name + relative
 * time header, content, and a chip row of reply / reaction counts.
 * One-tap reaction (default ❤️) lands without opening the detail page.
 *
 * The top of the page is a sticky inline composer entry that routes
 * to the standalone composer for full text + media. The page also
 * subscribes to the ``moment.*`` WS frames and refetches the inbox on
 * any update.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
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

const CONTENT_TRUNCATE_AT = 280
const DEFAULT_REACTION = '❤️'


function relativeTime(iso: string): string {
  const dt = new Date(iso)
  if (Number.isNaN(dt.getTime())) return iso
  const ms = Date.now() - dt.getTime()
  const m = Math.floor(ms / 60_000)
  if (m < 1)   return 'now'
  if (m < 60)  return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24)  return `${h}h`
  const d = Math.floor(h / 24)
  if (d < 7)   return `${d}d`
  return dt.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
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

  const blocked = blockedUserIds.value
  const visible = moments.value.filter(m => !blocked.has(m.author_user_id))
  const topLevel = visible.filter(m => !m.parent_moment_id)

  const quickReact = async (m: Moment, ev: Event) => {
    ev.preventDefault()
    ev.stopPropagation()
    // Optimistic bump + server PUT. The next refetch (post WS frame)
    // confirms the count.
    const idx = moments.value.findIndex(x => x.id === m.id)
    if (idx >= 0) {
      moments.value = moments.value.map((x, i) =>
        i === idx ? { ...x, reaction_count: x.reaction_count + 1 } : x,
      )
    }
    try {
      await api.put(`/api/moments/${m.id}/reaction`, { emoji: DEFAULT_REACTION })
    } catch (err: unknown) {
      // Roll back on failure.
      if (idx >= 0) {
        moments.value = moments.value.map((x, i) =>
          i === idx ? { ...x, reaction_count: Math.max(0, x.reaction_count - 1) } : x,
        )
      }
      showToast(`Reaction failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  return (
    <div class="sh-momentum">
      <header class="sh-momentum-header">
        <h2>Momentum</h2>
        <a href="/momentum/archive" class="sh-link sh-momentum-archive-link">
          📅 Archive
        </a>
      </header>

      {/* Inline composer entry — Twitter's "What's happening?" pattern.
          Tap routes to the full composer page; the inline box is
          intentionally read-only so we don't duplicate the rich-media
          flow inline. */}
      <button
        type="button"
        class="sh-momentum-compose-entry"
        onClick={() => loc.route('/momentum/new')}
      >
        <Avatar name={me ?? '?'} size={32} />
        <span class="sh-momentum-compose-prompt">What's on your mind?</span>
      </button>

      {topLevel.length === 0 && (
        <div class="sh-empty-state">
          <div style={{ fontSize: '2rem' }} aria-hidden="true">🌅</div>
          <h3 style={{ margin: 0 }}>No moments yet</h3>
          <p>
            A moment is a one-shot post that fans out to your paired
            households and theirs. They live 24 h by default, or 7 d
            for people you follow.
          </p>
        </div>
      )}

      <ul class="sh-momentum-list" aria-label="Moments">
        {topLevel.map(m => (
          <MomentRow
            key={m.id}
            m={m}
            mine={m.author_user_id === me}
            onOpen={() => loc.route(`/momentum/${m.id}`)}
            onReact={(ev) => void quickReact(m, ev)}
          />
        ))}
      </ul>
    </div>
  )
}


function MomentRow({
  m,
  mine,
  onOpen,
  onReact,
}: {
  m: Moment
  mine: boolean
  onOpen: () => void
  onReact: (ev: Event) => void
}) {
  const expanded = signal(false)
  const longContent = m.content.length > CONTENT_TRUNCATE_AT
  const visibleText = longContent && !expanded.value
    ? m.content.slice(0, CONTENT_TRUNCATE_AT) + '…'
    : m.content

  return (
    <li class="sh-momentum-row" onClick={onOpen}>
      <Avatar name={m.author_user_id} size={36} />
      <div class="sh-momentum-row-body">
        <div class="sh-momentum-row-head">
          <strong class="sh-momentum-row-author">
            {mine ? 'You' : m.author_user_id}
          </strong>
          <span class="sh-muted">· {relativeTime(m.created_at)}</span>
          {!mine && (
            <button
              type="button"
              class="sh-momentum-row-overflow"
              aria-label={`More actions for ${m.author_user_id}`}
              onClick={(ev) => {
                ev.preventDefault()
                ev.stopPropagation()
                openUserActions(m.author_user_id)
              }}
            >
              ⋯
            </button>
          )}
        </div>

        {m.content && (
          <p class="sh-momentum-row-content">
            {visibleText}
            {longContent && !expanded.value && (
              <button
                type="button"
                class="sh-momentum-row-more"
                onClick={(ev) => {
                  ev.preventDefault()
                  ev.stopPropagation()
                  expanded.value = true
                }}
              >
                Show more
              </button>
            )}
          </p>
        )}

        {m.media_type === 'image' && m.media_url && (
          <img
            src={m.media_url}
            alt=""
            loading="lazy"
            class="sh-momentum-row-media"
            onClick={(ev) => ev.stopPropagation()}
          />
        )}
        {m.media_type === 'video' && m.media_url && (
          <video
            src={m.media_url}
            controls
            muted
            preload="metadata"
            class="sh-momentum-row-media"
            onClick={(ev) => ev.stopPropagation()}
          />
        )}

        {/* Engagement chip row — Twitter-style icons + counts. */}
        <div class="sh-momentum-row-chips">
          <button
            type="button"
            class="sh-momentum-chip"
            aria-label={`${m.reply_count} replies`}
            onClick={onOpen}
          >
            💬 {m.reply_count > 0 ? m.reply_count : ''}
          </button>
          <button
            type="button"
            class="sh-momentum-chip"
            aria-label={`React ${DEFAULT_REACTION}`}
            disabled={mine}
            onClick={mine ? undefined : onReact}
          >
            {DEFAULT_REACTION} {m.reaction_count > 0 ? m.reaction_count : ''}
          </button>
        </div>
      </div>
    </li>
  )
}
