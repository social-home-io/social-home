import { useEffect, useRef } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useRoute, useLocation } from 'preact-iso'
import { api } from '@/api'
import { ws } from '@/ws'
import type { Message } from '@/types'
import { DmThreadSkeleton } from '@/components/Skeleton'
import { Button } from '@/components/Button'
import { SttButton } from '@/components/SttButton'
import { showToast } from '@/components/Toast'
import { ReadReceipt, readReceiptsEnabled } from '@/components/ReadReceipts'
import { TypingIndicator, sendTyping } from '@/components/TypingIndicator'
import { openCallTypePicker } from '@/components/CallTypePickerDialog'
import { currentUser } from '@/store/auth'
import { hasCapability } from '@/store/instance'

const messages = signal<Message[]>([])
const loading = signal(true)
/** True while a ``POST /api/conversations/{id}/messages`` is in
 *  flight. Disables the Send button + locks the input so the user
 *  can't double-submit a slow request (which previously fired off
 *  two copies of the same message). Mirrors the busy state to the
 *  composer chrome so the user has a clear "it's on the way" cue. */
const sending = signal(false)
const readMessageIds = signal<Set<string>>(new Set())
const deliveredMessageIds = signal<Set<string>>(new Set())
const memberCount = signal<number>(0)

interface ThreadMember {
  user_id: string
  username: string
  display_name: string
  is_self: boolean
  is_online: boolean
  is_idle: boolean
  last_seen_at: string | null
}

const threadMembers = signal<ThreadMember[]>([])
/** WhatsApp-style reply target. When set, the composer shows a chip
 *  with the parent message preview and the next send carries
 *  ``reply_to_id``. Cleared after send or by the chip's "×" button. */
const replyTo = signal<Message | null>(null)

/** WhatsApp-style "Last seen 12 min ago" formatter — same shape as the
 *  presence-page helper but inline so this page doesn't grow a util
 *  module just for one consumer. */
function humanizeAgo(iso: string | null | undefined): string | null {
  if (!iso) return null
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return null
  const sec = Math.max(0, Math.round((Date.now() - t) / 1000))
  if (sec < 60)      return 'just now'
  if (sec < 3600)    return `${Math.floor(sec / 60)} min ago`
  if (sec < 86400)   return `${Math.floor(sec / 3600)} h ago`
  return `${Math.floor(sec / 86400)} d ago`
}

/** Build the WhatsApp-style status line for the thread header.
 *  • 1:1 DM → peer's online state, or "last seen X" when offline.
 *  • Group DM → "<n> online" when ≥ 1 peer is online; otherwise null
 *    (group threads don't surface a per-peer last-seen line — too noisy). */
function statusLine(members: ThreadMember[]): string | null {
  const peers = members.filter(m => !m.is_self)
  if (peers.length === 0) return null
  if (peers.length === 1) {
    const p = peers[0]
    if (p.is_online && p.is_idle) return 'Idle'
    if (p.is_online)              return 'Online'
    const ago = humanizeAgo(p.last_seen_at)
    return ago ? `Last seen ${ago}` : 'Offline'
  }
  const onlineCount = peers.filter(p => p.is_online).length
  if (onlineCount === 0) return null
  return `${onlineCount} online`
}

interface DeliveryState {
  message_id: string
  user_id: string
  state: 'delivered' | 'read'
  state_at: string
}

interface MessageGap {
  sender_user_id: string
  expected_seq: number
  detected_at: string
}

const gaps = signal<MessageGap[]>([])

/**
 * Render a ``type="call_event"`` system message as a compact centred row
 * in the DM thread (spec §26.8). The backend stores a JSON blob describing
 * the event; we parse it at render-time and offer a one-tap "Call back"
 * on missed/declined events.
 */
function CallEventRow({ m, onCallBack }: { m: Message, onCallBack: (type: 'audio' | 'video') => void }) {
  let ev: { event?: string, call_type?: string, duration_seconds?: number | null } = {}
  try { ev = JSON.parse(m.content) } catch { /* noop */ }
  const ic = ev.call_type === 'video' ? '📹' : '📞'
  const label = ev.event === 'missed' ? 'Missed call'
    : ev.event === 'declined' ? 'Declined call'
    : ev.event === 'ended'    ? 'Call'
    : 'Call started'
  const dur = ev.duration_seconds && ev.duration_seconds > 0
    ? ` · ${formatDuration(ev.duration_seconds)}` : ''
  const when = new Date(m.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const showBack = ev.event === 'missed' || ev.event === 'declined'
  return (
    <div class="sh-call-event">
      <span class="sh-call-event-icon">{ic}</span>
      <span class="sh-call-event-label">{label}</span>
      <span class="sh-call-event-meta">{dur} · {when}</span>
      {showBack && (
        <Button onClick={() => onCallBack((ev.call_type as 'audio' | 'video') ?? 'audio')}>
          Call back
        </Button>
      )}
    </div>
  )
}

function formatDuration(sec: number): string {
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return m > 0 ? `${m}m ${s}s` : `${s}s`
}

/** Thread-header call button (§26.2).
 *
 * One phone icon — tapping it opens :class:`CallTypePickerDialog` so the
 * user picks audio vs. video on a focused dialog rather than having to
 * choose between two cramped header icons. The chosen type is fixed at
 * offer time on the backend; mid-call enable/disable of video is handled
 * by :func:`InCallPage.toggleCamera`.
 */
function CallButton({ convId }: { convId: string }) {
  // Only meaningful when the DM has ≥ 1 peer.
  if (memberCount.value < 2) return null
  return (
    <div class="sh-thread-call-buttons">
      <button type="button" class="sh-icon-btn" title="Start call"
              onClick={() => openCallTypePicker(convId)}
              aria-label={`Start call in conversation ${convId}`}>📞</button>
    </div>
  )
}

export default function DmThreadPage() {
  const { params } = useRoute()
  const convId = params.id
  const location = useLocation()
  // Composer ``<input>`` ref — STT (push-to-talk transcription) appends
  // its final transcript here so the user can review + edit before
  // sending. Uncontrolled input + ref keeps the existing FormData send
  // path untouched.
  const composerInputRef = useRef<HTMLInputElement | null>(null)
  // Scrolling container for the messages list. We pin the view to the
  // bottom on first load, and auto-stick to bottom on new messages
  // when the user is already there (so an active conversation
  // follows itself without yanking a reader who scrolled up to
  // read older context).
  const messagesScrollRef = useRef<HTMLDivElement | null>(null)
  const stickToBottom = useRef(true)

  // Tag the body so the mobile layout can hide the bottom tab bar and
  // full-bleed the thread — the chat surface should claim the whole
  // viewport on small screens. The class is scoped to the DM thread
  // route via the useEffect lifecycle.
  useEffect(() => {
    document.body.classList.add('sh-dm-thread-open')
    return () => document.body.classList.remove('sh-dm-thread-open')
  }, [])

  useEffect(() => {
    loading.value = true
    api.get(`/api/conversations/${convId}/messages`).then(data => {
      messages.value = data.reverse()
      loading.value = false
      if (readReceiptsEnabled.value) {
        api.post(`/api/conversations/${convId}/read`).catch(() => {})
      }
      // Hydrate delivery/read state for every message so ticks render
      // immediately — not just on messages we've seen WS frames for.
      api.get(`/api/conversations/${convId}/delivery-states`).then(
        (body: { states: DeliveryState[] }) => {
          const delivered = new Set<string>()
          const read = new Set<string>()
          for (const s of body.states || []) {
            if (s.state === 'read') read.add(s.message_id)
            else if (s.state === 'delivered') delivered.add(s.message_id)
          }
          deliveredMessageIds.value = delivered
          readMessageIds.value = new Set([...readMessageIds.value, ...read])
        },
      ).catch(() => {})
      // Poll for open sequence gaps — tiny endpoint, once per thread load.
      api.get(`/api/conversations/${convId}/gaps`).then(
        (body: { gaps: MessageGap[] }) => {
          gaps.value = body.gaps || []
        },
      ).catch(() => { gaps.value = [] })
    })
    // Roster fetch — drives the call-button visibility (member_count) AND
    // the WhatsApp-style "Online" / "Last seen 2 h ago" status line in the
    // thread header. Live-patched below by the user.online/idle/offline
    // WS frames so the header stays current without polling.
    api.get(`/api/conversations/${convId}/members`).then((rows: ThreadMember[]) => {
      threadMembers.value = rows
      memberCount.value = rows.length || 2
    }).catch(() => {
      threadMembers.value = []
      memberCount.value = 2
    })

    const offRead = ws.on('dm.read', (evt) => {
      const data = evt.data as { conversation_id?: string; message_ids?: string[] }
      if (data.conversation_id === convId && data.message_ids) {
        readMessageIds.value = new Set([...readMessageIds.value, ...data.message_ids])
      }
    })
    const offNewMsg = ws.on('dm.message', (evt) => {
      const data = evt.data as { conversation_id?: string; message?: Message }
      if (data.conversation_id === convId && data.message) {
        const msg = data.message
        if (!messages.value.some(m => m.id === msg.id)) {
          messages.value = [...messages.value, msg]
          // Ack delivery as soon as the frame lands. The server upsert
          // is idempotent; a later ``read`` supersedes.
          const mine = msg.sender_user_id === currentUser.value?.user_id
          if (!mine && readReceiptsEnabled.value) {
            api.post(
              `/api/conversations/${convId}/messages/${msg.id}/delivered`,
            ).catch(() => {})
          }
        }
        if (readReceiptsEnabled.value) {
          api.post(`/api/conversations/${convId}/read`).catch(() => {})
        }
      }
    })
    // Live-patch the thread-member roster on session-presence frames so
    // the header status line stays current.
    const patchMember = (
      user_id: string,
      next: { is_online: boolean; is_idle: boolean; last_seen_at?: string | null },
    ) => {
      threadMembers.value = threadMembers.value.map(m =>
        m.user_id === user_id
          ? {
              ...m,
              is_online: next.is_online,
              is_idle: next.is_idle,
              ...(next.last_seen_at !== undefined ? { last_seen_at: next.last_seen_at } : {}),
            }
          : m,
      )
    }
    const offUserOnline = ws.on('user.online', (e) => {
      const d = e.data as { user_id?: string }
      if (d.user_id) patchMember(d.user_id, { is_online: true, is_idle: false })
    })
    const offUserIdle = ws.on('user.idle', (e) => {
      const d = e.data as { user_id?: string }
      if (d.user_id) patchMember(d.user_id, { is_online: true, is_idle: true })
    })
    const offUserOffline = ws.on('user.offline', (e) => {
      const d = e.data as { user_id?: string; last_seen_at?: string | null }
      if (d.user_id) patchMember(d.user_id, {
        is_online: false,
        is_idle: false,
        last_seen_at: d.last_seen_at ?? null,
      })
    })
    return () => {
      offRead(); offNewMsg()
      offUserOnline(); offUserIdle(); offUserOffline()
    }
  }, [convId])

  let typingTimer: ReturnType<typeof setTimeout> | null = null

  const handleInput = () => {
    if (typingTimer) return
    sendTyping(convId)
    typingTimer = setTimeout(() => { typingTimer = null }, 2000)
  }

  const handleSend = async (e: Event) => {
    e.preventDefault()
    if (sending.value) return  // belt-and-braces guard for keyboard Enter
    const form = e.target as HTMLFormElement
    const content = new FormData(form).get('content') as string
    if (!content.trim()) return
    const reply_to_id = replyTo.value?.id ?? null
    sending.value = true
    // Optimistically clear the input + reply chip so the user gets the
    // same "I pressed send, the message is gone" feedback iMessage /
    // WhatsApp give. The Button's loading spinner + the input's
    // ``disabled`` state make the in-flight state unambiguous. On
    // failure we restore the draft so nothing is silently lost.
    const draft = content
    form.reset()
    const restoredReply = replyTo.value
    replyTo.value = null
    try {
      await api.post(`/api/conversations/${convId}/messages`, {
        content,
        ...(reply_to_id ? { reply_to_id } : {}),
      })
      const data = await api.get(`/api/conversations/${convId}/messages`)
      messages.value = data.reverse()
    } catch (err: unknown) {
      // Restore the draft so the user can retry without re-typing.
      const input = (form.elements.namedItem('content') as HTMLInputElement | null)
      if (input) {
        input.value = draft
        input.focus()
      }
      replyTo.value = restoredReply
      showToast(
        `Send failed: ${(err as Error)?.message ?? err}`,
        'error',
      )
    } finally {
      sending.value = false
    }
  }

  /** Resolve sender display name from the roster — falls back to the raw
   *  user_id (which the rest of the thread also surfaces today). */
  const senderName = (user_id: string): string => {
    const m = threadMembers.value.find(x => x.user_id === user_id)
    return m?.display_name ?? m?.username ?? user_id
  }

  /** One-line preview of a message's content for the quoted-reply card.
   *  Strips newlines and truncates to keep the bubble compact. */
  const quotePreview = (m: Message): string => {
    if (m.deleted) return '(message deleted)'
    if (!m.content) return m.media_url ? '📎 Attachment' : ''
    const flat = m.content.replace(/\s+/g, ' ').trim()
    return flat.length > 80 ? `${flat.slice(0, 80)}…` : flat
  }

  /** Scroll the original message into view + flash it briefly so the
   *  reply quote is genuinely useful as a navigation handle. */
  const scrollToMessage = (id: string) => {
    const el = document.querySelector<HTMLElement>(`[data-msg-id="${id}"]`)
    if (!el) return
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    el.classList.add('sh-message--flash')
    setTimeout(() => el.classList.remove('sh-message--flash'), 1200)
  }

  const startCall = async (callType: 'audio' | 'video') => {
    // For v1 the backend expects a placeholder SDP — the real offer is
    // generated by ``InCallPage`` on mount via ``RtcTransport``.
    const r = await api.post('/api/calls', {
      conversation_id: convId,
      call_type: callType,
      sdp_offer: 'v=0\r\n',
    }) as { call_id: string }
    location.route(`/calls/${r.call_id}`)
  }

  if (loading.value) return <DmThreadSkeleton />
  const myUserId = currentUser.value?.user_id

  // Scroll the messages list to the bottom whenever the page first
  // settles after a load (so opening a chat shows the most recent
  // messages, like every other chat app) or whenever a new message
  // lands while the user is already at the bottom.
  const messageCount = messages.value.length
  useEffect(() => {
    const el = messagesScrollRef.current
    if (!el) return
    if (!stickToBottom.current) return
    // Two frames: first to let the new bubble render, second so the
    // scroll happens *after* the layout settles. Without the rAF the
    // assignment runs before the bubble's height is accounted for and
    // the scroll lands a few px short. ``behavior: 'auto'`` forces an
    // instant jump even though the container has ``scroll-behavior:
    // smooth`` in CSS — opening a chat should land at the bottom
    // immediately, not glide there over half a second.
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        el.scrollTo({ top: el.scrollHeight, behavior: 'auto' })
      })
    })
  }, [messageCount])

  const handleScroll = () => {
    const el = messagesScrollRef.current
    if (!el) return
    // 80 px of slack — anything closer counts as "the user is at
    // the bottom and wants to keep following".
    const dist = el.scrollHeight - (el.scrollTop + el.clientHeight)
    stickToBottom.current = dist < 80
  }

  // The ``messageCount`` effect above re-scrolls when a new message
  // row lands, but the typing indicator is rendered inside the same
  // container *without* changing ``messages.value.length`` — it's
  // driven by a separate signal in ``TypingIndicator``. Without an
  // observer the indicator can mount just below the viewport and
  // the user never sees it. A ``MutationObserver`` on the container
  // catches that case (and any other DOM growth — long messages
  // finishing layout, delivery-tick re-renders) and re-pins the
  // scroll to the bottom when the user was already there.
  useEffect(() => {
    const el = messagesScrollRef.current
    if (!el) return
    const mo = new MutationObserver(() => {
      if (!stickToBottom.current) return
      requestAnimationFrame(() => {
        el.scrollTo({ top: el.scrollHeight, behavior: 'auto' })
      })
    })
    mo.observe(el, { childList: true, subtree: true, characterData: true })
    return () => mo.disconnect()
  }, [])

  const status = statusLine(threadMembers.value)
  // Compact status modifier for the dot in the header: 'online' → green,
  // 'idle' → amber, anything else → no dot.
  const peers = threadMembers.value.filter(m => !m.is_self)
  const headerDot: 'online' | 'idle' | null = peers.length === 1
    ? (peers[0].is_online ? (peers[0].is_idle ? 'idle' : 'online') : null)
    : (peers.some(p => p.is_online) ? 'online' : null)

  return (
    <div class="sh-thread">
      <div class="sh-thread-header">
        {/* Mobile-only back arrow. Desktop already has the sidebar.
         * Hidden via CSS on ≥769 px so it doesn't fight with the
         * desktop "Chats" sidebar link for the same affordance. */}
        <a
          class="sh-thread-back"
          href="/dms"
          aria-label="Back to chats"
        >‹</a>
        <div class="sh-thread-header-status" aria-live="polite">
          {headerDot && (
            <span class={`sh-thread-header-dot sh-thread-header-dot--${headerDot}`}
                  aria-hidden="true" />
          )}
          {status && <span class="sh-thread-header-status-line">{status}</span>}
        </div>
        <CallButton convId={convId} />
        <a
          class="sh-thread-history"
          href={`/dms/${convId}/calls`}
          title="Call history"
          aria-label="Call history"
        >
          <span aria-hidden="true">🕘</span>
        </a>
      </div>
      {gaps.value.length > 0 && (
        <div class="sh-dm-gap-banner" role="status" aria-live="polite">
          <span aria-hidden="true">⚠️</span>
          <span>
            {gaps.value.length === 1
              ? 'A message may be missing from this conversation.'
              : `${gaps.value.length} messages may be missing from this conversation.`}
            {' '}Ask the sender to repost if it looks wrong.
          </span>
        </div>
      )}
      <div class="sh-messages" ref={messagesScrollRef} onScroll={handleScroll}>
        {messages.value.map(m => {
          if (m.type === 'call_event') {
            return <CallEventRow key={m.id} m={m} onCallBack={startCall} />
          }
          const mine = m.sender_user_id === myUserId
          // Look up the parent message for inline rendering of the
          // quoted-reply card. Missing parents (loaded out of window or
          // soft-deleted) fall through to a small placeholder.
          const parent = m.reply_to_id
            ? messages.value.find(x => x.id === m.reply_to_id)
            : null
          return (
            <div
              key={m.id}
              data-msg-id={m.id}
              class={`sh-message ${mine ? 'sh-message--mine' : ''} ${m.deleted ? 'sh-message--deleted' : ''}`}
            >
              {!mine && <strong>{senderName(m.sender_user_id)}</strong>}
              {m.reply_to_id && (
                <button
                  type="button"
                  class="sh-message-quote"
                  onClick={() => parent && scrollToMessage(parent.id)}
                  aria-label={parent
                    ? `Reply to ${senderName(parent.sender_user_id)}: ${quotePreview(parent)}`
                    : 'Reply to a message'}
                >
                  <span class="sh-message-quote-author">
                    {parent ? senderName(parent.sender_user_id) : 'Unknown'}
                  </span>
                  <span class="sh-message-quote-body">
                    {parent ? quotePreview(parent) : '(message unavailable)'}
                  </span>
                </button>
              )}
              <p style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
                {m.deleted ? '(message deleted)' : m.content}
              </p>
              <div class="sh-message-meta">
                <time>{new Date(m.created_at).toLocaleTimeString([],
                  { hour: '2-digit', minute: '2-digit' })}</time>
                {mine && (
                  <ReadReceipt
                    sent={true}
                    delivered={
                      deliveredMessageIds.value.has(m.id) ||
                      readMessageIds.value.has(m.id)
                    }
                    read={readMessageIds.value.has(m.id)}
                  />
                )}
              </div>
              {!m.deleted && (
                <button
                  type="button"
                  class="sh-message-reply-btn"
                  title="Reply"
                  aria-label={`Reply to ${senderName(m.sender_user_id)}`}
                  onClick={() => { replyTo.value = m }}
                >
                  ↩
                </button>
              )}
            </div>
          )
        })}
        {/* In-thread typing indicator: rendered as the last "row" of
         * the messages list so it reads like a chat-bubble preview
         * (WhatsApp / iMessage), not a chrome strip above the
         * composer where it used to disappear on first glance. The
         * ``bubble`` prop opts into the bubble shape. */}
        <TypingIndicator scope={convId} bubble />
      </div>
      {replyTo.value && (
        <div class="sh-composer-reply" role="status" aria-live="polite">
          <div class="sh-composer-reply-body">
            <span class="sh-composer-reply-author">
              Replying to {senderName(replyTo.value.sender_user_id)}
            </span>
            <span class="sh-composer-reply-preview">
              {quotePreview(replyTo.value)}
            </span>
          </div>
          <button
            type="button"
            class="sh-composer-reply-clear"
            aria-label="Cancel reply"
            onClick={() => { replyTo.value = null }}
          >×</button>
        </div>
      )}
      <form
        class={
          'sh-composer'
          + (sending.value ? ' sh-composer--sending' : '')
        }
        onSubmit={handleSend}
      >
        <input
          ref={composerInputRef}
          name="content"
          placeholder={sending.value ? 'Sending…' : 'Type a message...'}
          autocomplete="off"
          // Lock the input while the POST is in flight so a fast
          // typist can't keep adding to a message that's already
          // being delivered.
          disabled={sending.value}
          onInput={handleInput}
        />
        {hasCapability('stt') && (
          <SttButton
            onText={(t) => {
              const input = composerInputRef.current
              if (!input) return
              const cur = input.value
              const sep = cur && !/\s$/.test(cur) ? ' ' : ''
              input.value = cur + sep + t
              // Nudge the typing indicator + any input listeners.
              input.dispatchEvent(new Event('input', { bubbles: true }))
              input.focus()
            }}
          />
        )}
        <Button
          type="submit"
          loading={sending.value}
          aria-label={sending.value ? 'Sending message' : 'Send message'}
        >
          {/* Compact paper-plane icon so the composer reads as a chat
           *  bar (most of the row goes to the text input) rather than
           *  a form with a wide CTA. Loading spinner replaces the
           *  glyph via the ``Button`` component's ``loading`` prop. */}
          <span aria-hidden="true" class="sh-composer-send-icon">➤</span>
        </Button>
      </form>
    </div>
  )
}
