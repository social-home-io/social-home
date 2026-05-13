/**
 * MomentumInboxTab — inbox for the Momentum pillar (§Momentum).
 *
 * Twitter-style row layout: tight rows with an inline name + relative
 * time header, content, and a chip row of reply / reaction counts.
 * One-tap reaction (default ❤️) lands without opening the detail page.
 *
 * The top of the tab is a sticky inline composer entry that routes
 * to the standalone composer for full text + media. The tab also
 * subscribes to the ``moment.*`` WS frames and refetches the inbox on
 * any update. The host :class:`MomentumPage` switches between this
 * and :class:`MomentumArchiveTab`.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { openLightbox } from '@/components/ImageLightbox'
import { openMomentumComposer } from '@/components/MomentumComposerDialog'
import { MomentumInboxSkeleton } from '@/components/Skeleton'
import { showToast } from '@/components/Toast'
import { openUserActions } from '@/components/UserActionsMenu'
import { blockedUserIds, loadBlocks } from '@/store/blocks'
import {
  householdDisplayName,
  householdPictureUrl,
  loadHouseholdUsers,
} from '@/store/householdUsers'
import { currentUser } from '@/store/auth'
import { ws } from '@/ws'
import type { Moment } from '@/types'
import { renderHashtagged } from './hashtags'

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


export default function MomentumInboxTab() {
  const loc = useLocation()
  const me = currentUser.value?.user_id

  useEffect(() => {
    loading.value = true
    void loadBlocks()
    void loadHouseholdUsers()  // resolve display names + avatars from raw user_ids
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

  if (loading.value) return <MomentumInboxSkeleton />

  const blocked = blockedUserIds.value
  const visible = moments.value.filter(m => !blocked.has(m.author_user_id))
  const topLevel = visible.filter(m => !m.parent_moment_id)

  const myDisplayName = me ? householdDisplayName(me) : '?'
  const myPicture = householdPictureUrl(me)

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
      {/* Inline composer entry — Twitter's "What's happening?" pattern.
          Tap routes to the full composer page; the inline box is
          intentionally read-only so we don't duplicate the rich-media
          flow inline. */}
      <button
        type="button"
        class="sh-momentum-compose-entry"
        onClick={() => openMomentumComposer()}
      >
        <Avatar name={myDisplayName} src={myPicture} size={32} />
        <span class="sh-momentum-compose-prompt">What's on your mind?</span>
      </button>

      {topLevel.length === 0 && (
        <div class="sh-empty-state">
          <div aria-hidden="true">🌅</div>
          <h3>No moments yet</h3>
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
            authorName={householdDisplayName(m.author_user_id)}
            authorPicture={householdPictureUrl(m.author_user_id)}
            onOpen={() => loc.route(`/momentum/${m.id}`)}
            onReact={(ev) => void quickReact(m, ev)}
            onTagClick={(t) => loc.route(`/momentum?tab=archive&tag=${encodeURIComponent(t)}`)}
          />
        ))}
      </ul>
    </div>
  )
}


function MomentRow({
  m,
  mine,
  authorName,
  authorPicture,
  onOpen,
  onReact,
  onTagClick,
}: {
  m: Moment
  mine: boolean
  authorName: string
  authorPicture: string | null
  onOpen: () => void
  onReact: (ev: Event) => void
  onTagClick: (tag: string) => void
}) {
  const expanded = signal(false)
  const longContent = m.content.length > CONTENT_TRUNCATE_AT
  const visibleText = longContent && !expanded.value
    ? m.content.slice(0, CONTENT_TRUNCATE_AT) + '…'
    : m.content

  return (
    <li class="sh-momentum-row" onClick={onOpen}>
      <Avatar name={authorName} src={authorPicture} size={36} />
      <div class="sh-momentum-row-body">
        <div class="sh-momentum-row-head">
          <strong class="sh-momentum-row-author">
            {mine ? 'You' : authorName}
          </strong>
          <span class="sh-muted">· {relativeTime(m.created_at)}</span>
          {m.received_via === 'gfs' && (
            <span
              class="sh-momentum-row-via-gfs"
              title="Received via a public-share GFS"
            >
              · via GFS
            </span>
          )}
          {!mine && (
            <button
              type="button"
              class="sh-momentum-row-overflow"
              aria-label={`More actions for ${authorName}`}
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
            {renderHashtagged(visibleText, (t, ev) => {
              ev.preventDefault()
              ev.stopPropagation()
              onTagClick(t)
            })}
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
          <button
            type="button"
            class="sh-momentum-row-media-button"
            aria-label="Open photo full-size"
            onClick={(ev) => {
              ev.preventDefault()
              ev.stopPropagation()
              openLightbox({
                items: [{
                  url:       m.media_url!,
                  item_type: 'photo',
                  caption:   m.content || null,
                }],
              })
            }}
          >
            <img
              src={m.media_url}
              alt={m.content ? '' : `Photo from ${authorName}`}
              loading="lazy"
              class="sh-momentum-row-media"
            />
          </button>
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
