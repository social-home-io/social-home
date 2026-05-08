/**
 * MomentumDetailPage — single moment + replies + reactions.
 *
 * Routed at ``/momentum/{momentId}``. Subscribes to ``moment.*`` WS
 * frames and refetches on any update affecting this moment id (new
 * reply, reaction change, deletion).
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation, useRoute } from 'preact-iso'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import {
  MomentumComposerDialog,
  openMomentumComposer,
} from '@/components/MomentumComposerDialog'
import { MomentumDetailSkeleton } from '@/components/Skeleton'
import { showToast } from '@/components/Toast'
import { confirmDialog } from '@/components/confirm'
import { openReport } from '@/components/ReportDialog'
import { openUserActions } from '@/components/UserActionsMenu'
import { currentUser } from '@/store/auth'
import {
  householdDisplayName,
  householdPictureUrl,
  loadHouseholdUsers,
} from '@/store/householdUsers'
import { useTitle } from '@/store/pageTitle'
import { ws } from '@/ws'
import type { Moment, MomentDetail } from '@/types'
import { renderHashtagged } from './hashtags'

const QUICK_REACTIONS = ['❤️', '🔥', '😂', '😮', '😢', '👏'] as const

const detail = signal<MomentDetail | null>(null)
const loading = signal<boolean>(true)


export default function MomentumDetailPage() {
  useTitle('Moment')
  const { params } = useRoute()
  const loc = useLocation()
  const momentId = params.momentId
  const me = currentUser.value?.user_id

  useEffect(() => {
    loading.value = true
    detail.value = null
    void loadHouseholdUsers()  // resolve display names + avatars from raw user_ids
    const refetch = (initial: boolean) =>
      api.get(`/api/moments/${momentId}`)
        .then((d: MomentDetail) => {
          detail.value = d
          if (initial) loading.value = false
        })
        .catch((err: unknown) => {
          if (initial) {
            showToast(`Couldn't load: ${(err as Error)?.message ?? err}`,
              'error')
            loc.route('/momentum')
          }
        })
    void refetch(true)
    const matches = (data: { moment_id?: string; parent_moment_id?: string | null }) =>
      data.moment_id === momentId || data.parent_moment_id === momentId
    const dispose = [
      ws.on('moment.created',          (e) => {
        if (matches(e.data as { parent_moment_id?: string })) void refetch(false)
      }),
      ws.on('moment.deleted',          (e) => {
        if (matches(e.data as { moment_id?: string })) void refetch(false)
      }),
      ws.on('moment.reaction_changed', (e) => {
        if (matches(e.data as { moment_id?: string })) void refetch(false)
      }),
    ]
    return () => { dispose.forEach(d => d()) }
  }, [momentId])

  if (loading.value) return <MomentumDetailSkeleton />
  if (!detail.value) return null
  const m = detail.value.moment
  const isAuthor = m.author_user_id === me

  const react = async (emoji: string) => {
    try {
      await api.put(`/api/moments/${m.id}/reaction`, { emoji })
    } catch (err: unknown) {
      showToast(`Reaction failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  const clearReaction = async () => {
    try {
      await api.delete(`/api/moments/${m.id}/reaction`)
    } catch (err: unknown) {
      showToast(`Couldn't clear: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  const remove = async () => {
    if (!await confirmDialog('Delete this moment?', { destructive: true })) return
    try {
      await api.delete(`/api/moments/${m.id}`)
      showToast('Moment deleted', 'info')
      loc.route('/momentum')
    } catch (err: unknown) {
      showToast(`Delete failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  const report = () => openReport('moment', m.id)

  const myReaction = detail.value.reactions.find(
    r => r.reactor_user_id === me,
  )?.emoji
  // Roll up other reactions into a count map for the chip row.
  const counts: Record<string, number> = {}
  for (const r of detail.value.reactions) {
    counts[r.emoji] = (counts[r.emoji] || 0) + 1
  }

  const renderRow = (mm: Moment) => (
    <li key={mm.id} class="sh-momentum-row">
      <Avatar
        name={householdDisplayName(mm.author_user_id)}
        src={householdPictureUrl(mm.author_user_id)}
        size={32}
      />
      <div class="sh-momentum-row-body">
        <div class="sh-momentum-row-meta">
          <strong>
            {mm.author_user_id === me ? 'You' : householdDisplayName(mm.author_user_id)}
          </strong>
          <span class="sh-muted">{mm.created_at.slice(11, 16)}</span>
        </div>
        {mm.content && (
          <p class="sh-momentum-row-content">
            {renderHashtagged(mm.content, (t, ev) => {
              ev.preventDefault()
              ev.stopPropagation()
              loc.route(`/momentum?tab=archive&tag=${encodeURIComponent(t)}`)
            })}
          </p>
        )}
        {mm.media_type === 'image' && mm.media_url && (
          <img src={mm.media_url} alt="" loading="lazy"
            class="sh-momentum-row-media" />
        )}
        {mm.media_type === 'video' && mm.media_url && (
          <video src={mm.media_url} controls muted preload="metadata"
            class="sh-momentum-row-media" />
        )}
      </div>
    </li>
  )

  return (
    <div class="sh-momentum-detail">
      <header class="sh-momentum-detail-header">
        <Avatar
          name={householdDisplayName(m.author_user_id)}
          src={householdPictureUrl(m.author_user_id)}
          size={48}
        />
        <div class="sh-momentum-detail-meta">
          <strong>{isAuthor ? 'You' : householdDisplayName(m.author_user_id)}</strong>
          <span class="sh-muted">
            {new Date(m.created_at).toLocaleString()}
          </span>
        </div>
        {!isAuthor && (
          <button
            type="button"
            class="sh-momentum-row-overflow"
            aria-label={`More actions for ${householdDisplayName(m.author_user_id)}`}
            onClick={() => openUserActions(m.author_user_id)}
          >
            ⋯
          </button>
        )}
        <Button variant="ghost" onClick={() => loc.route('/momentum')}>
          Close
        </Button>
      </header>

      {m.content && (
        <p class="sh-momentum-detail-content">
          {renderHashtagged(m.content, (t, ev) => {
            ev.preventDefault()
            ev.stopPropagation()
            loc.route(`/momentum?tab=archive&tag=${encodeURIComponent(t)}`)
          })}
        </p>
      )}
      {m.media_type === 'image' && m.media_url && (
        <img src={m.media_url} alt="" class="sh-momentum-detail-media" />
      )}
      {m.media_type === 'video' && m.media_url && (
        <video src={m.media_url} controls class="sh-momentum-detail-media" />
      )}

      <section class="sh-momentum-reactions" aria-label="Reactions">
        <div class="sh-momentum-reaction-counts">
          {Object.entries(counts).map(([emoji, n]) => (
            <span key={emoji} class="sh-momentum-reaction-count">
              {emoji} {n}
            </span>
          ))}
        </div>
        <div class="sh-momentum-reaction-picker">
          {QUICK_REACTIONS.map(emoji => (
            <button
              key={emoji}
              type="button"
              class={
                myReaction === emoji
                  ? 'sh-momentum-reaction-btn sh-momentum-reaction-btn--mine'
                  : 'sh-momentum-reaction-btn'
              }
              onClick={() => void (myReaction === emoji
                ? clearReaction()
                : react(emoji))}
              aria-label={`React ${emoji}`}
            >
              {emoji}
            </button>
          ))}
          {myReaction && (
            <Button variant="ghost" onClick={clearReaction}>
              Clear my reaction
            </Button>
          )}
        </div>
      </section>

      <footer class="sh-momentum-detail-actions">
        <Button onClick={() => openMomentumComposer(m.id)}>
          💬 Reply
        </Button>
        {!isAuthor && (
          <Button variant="ghost" onClick={report}>🚩 Report</Button>
        )}
        {isAuthor && (
          <Button variant="danger" onClick={remove}>Delete</Button>
        )}
      </footer>

      {detail.value.replies.length > 0 && (
        <section class="sh-momentum-replies" aria-label="Replies">
          <h3>
            {detail.value.replies.length === 1
              ? '1 reply'
              : `${detail.value.replies.length} replies`}
          </h3>
          <ul class="sh-momentum-list">
            {detail.value.replies.map(renderRow)}
          </ul>
        </section>
      )}
      {/* Mounted at the page tail so the Reply button can open the
       *  shared dialog. The WS ``moment.created`` listener already
       *  refetches the thread when the new reply lands, so no
       *  ``onPosted`` callback is needed here. */}
      <MomentumComposerDialog />
    </div>
  )
}
