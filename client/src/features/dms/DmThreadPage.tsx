import { Fragment } from 'preact'
import { useEffect, useRef, useLayoutEffect } from 'preact/hooks'
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
import { UnreadDivider } from '@/components/UnreadDivider'
import { openCallTypePicker } from '@/components/CallTypePickerDialog'
import { EmojiPickButton } from '@/components/EmojiPickButton'
import {
  EmojiAutocomplete,
  checkForEmojiTrigger,
  closeEmojiAutocomplete,
  handleEmojiAutocompleteKey,
} from '@/components/EmojiAutocomplete'
import { emojiByShortcode } from '@/data/emojis'
import { currentUser } from '@/store/auth'
import { hasCapability } from '@/store/instance'
import { useTitle } from '@/store/pageTitle'
import { normaliseTimestamp } from '@/utils/relativeTime'

const messages = signal<Message[]>([])
const loading = signal(true)
/** Page size for the lazy-load older-history fetch. The initial
 *  load uses a wider window (see ``DmThreadPage`` body); each
 *  follow-up "load older" page is this many messages. Backend caps
 *  any ``limit`` query at 100; 50 is the sweet spot — wide enough
 *  that you rarely need three fetches in one scroll session,
 *  narrow enough to feel instant on slow links. */
const PAGE_SIZE = 50
/** Whether older messages are available to fetch via
 *  ``?before=<oldest_id>``. Set to ``false`` when a fetch returns
 *  fewer messages than the requested limit (= no more history). */
const hasMoreHistory = signal(true)
/** True while a back-fill ``loadOlder()`` request is in flight.
 *  Drives the spinner at the top of the messages list and stops
 *  the scroll handler from queueing parallel requests. */
const isLoadingOlder = signal(false)
/** First-unread anchor used to render a "New messages" divider on
 *  entry. ``message_id`` is the id of the first message the caller
 *  hasn't read yet; the SPA scrolls that row into view. ``null`` if
 *  there are no unread messages in the loaded window, in which case
 *  the entry effect falls back to scroll-to-bottom. */
const unreadAnchor = signal<{ message_id: string } | null>(null)
/** Counter of new messages received since the user scrolled up off
 *  the bottom. Drives the "↓ N new messages" jump-down chip. Resets
 *  to zero when the user reaches the bottom (either by scrolling or
 *  by clicking the chip). */
const newSinceScrollUp = signal(0)
/** Cap how tall the composer textarea is allowed to grow before it
 *  starts to scroll internally. ~6 lines at the default font; matches
 *  WhatsApp's ceiling so a very long draft doesn't eat half the chat
 *  while the user is still typing. */
const MAX_COMPOSER_HEIGHT_PX = 160
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
  const composerInputRef = useRef<HTMLTextAreaElement | null>(null)
  /** Scrolling container for the messages list.
   *
   *  The container is laid out with ``flex-direction: column-reverse``
   *  (see ``.sh-messages`` in ``app.css``), which inverts the
   *  scroll coordinate system: ``scrollTop=0`` is the visual
   *  **bottom** (latest message), and scrolling up *increases*
   *  ``scrollTop``. This is the classic chat-app trick — entry
   *  needs no positioning effect because the browser naturally
   *  lands the user at the bottom, new messages appear at the
   *  bottom without any JS scroll, and prepending older history
   *  doesn't move the user's viewport because ``scrollTop`` is
   *  anchored relative to the visual bottom (not the visual top).
   *  All the scroll-position-restoration math the old code carried
   *  goes away. */
  const messagesScrollRef = useRef<HTMLDivElement | null>(null)
  /** ``true`` when the user is within 80 px of the visual bottom —
   *  i.e. they're "looking at the live edge" of the conversation.
   *  Drives the jump-down CTA visibility and the read-watermark
   *  advance: marks-as-read fire only when the user is actually
   *  caught up. In column-reverse, "near the bottom" means
   *  ``scrollTop < 80`` (recall: scrollTop=0 is the visual bottom).
   *  Maintained by ``handleScroll``. */
  const stickToBottom = useRef(true)

  // Tag the body so the layout can hide the bottom tab bar and
  // full-bleed the thread — the chat surface should claim the whole
  // viewport. The class is scoped to the DM thread route via the
  // useEffect lifecycle.
  useEffect(() => {
    document.body.classList.add('sh-dm-thread-open')
    return () => document.body.classList.remove('sh-dm-thread-open')
  }, [])

  const messageCount = messages.value.length
  const lastTailMessageId = useRef<string | null>(null)
  const isLoading = loading.value

  // Count new appended messages while the user is scrolled up so the
  // jump-down CTA can show "↓ N new". No scroll positioning here —
  // ``column-reverse`` handles "new messages naturally appear at the
  // visual bottom" for free; the only thing we need to track is
  // whether to surface the CTA. Self-sends set ``stickToBottom``
  // back to true in ``handleSend`` so they take the "user is at
  // bottom" branch and don't bump the counter. Prepended older
  // history (from ``loadOlder``) doesn't change the tail message id
  // so the ``tailChanged`` discriminator skips it.
  useEffect(() => {
    const tailId = messages.value.length > 0
      ? messages.value[messages.value.length - 1].id
      : null
    const tailChanged = tailId !== null && tailId !== lastTailMessageId.current
    lastTailMessageId.current = tailId
    if (!tailChanged || stickToBottom.current) return
    newSinceScrollUp.value += 1
  }, [messageCount])

  // Entry-scroll effect — only meaningful when there's an unread
  // anchor. With column-reverse, the no-anchor case is automatic:
  // the browser lands the user at ``scrollTop=0`` (visual bottom =
  // latest message). For the unread-anchor case we still need to
  // bring the "New messages" divider into view, so we scroll up
  // until the divider is at the top of the viewport. ``useLayoutEffect``
  // runs after Preact's DOM mutations but before the browser paints,
  // so the user never sees the intermediate scrollTop=0 frame
  // (which would show the latest message instead of the divider).
  const anchor = unreadAnchor.value
  useLayoutEffect(() => {
    if (isLoading || !anchor) return
    const el = messagesScrollRef.current
    if (!el) return
    stickToBottom.current = false
    const divider = el.querySelector('.sh-dm-unread-divider')
    if (divider) {
      divider.scrollIntoView({ block: 'start', behavior: 'instant' })
    } else {
      // Race fallback — anchor row rendered but divider didn't.
      const row = el.querySelector(`[data-msg-id="${anchor.message_id}"]`)
      if (row) row.scrollIntoView({ block: 'start', behavior: 'instant' })
    }
  }, [convId, isLoading, anchor?.message_id])

  useEffect(() => {
    loading.value = true
    // Reset the lazy-load + anchor state for the new thread. Without
    // this a re-entry would inherit the previous thread's divider or
    // "no more history" flag, both wrong for the new context.
    hasMoreHistory.value = true
    isLoadingOlder.value = false
    unreadAnchor.value = null
    newSinceScrollUp.value = 0
    // Reset messages eagerly so the brief moment between the convId
    // change and the new fetch's ``then`` doesn't flash the previous
    // thread's content. Column-reverse means the empty list shows
    // an empty container at scrollTop=0 (visual bottom) and the
    // loading pill below.
    messages.value = []
    stickToBottom.current = true
    // Look up this thread's row in the conversations list to read
    // ``unread`` + ``last_read_at`` — the SPA uses both to size the
    // initial message window (so the first-unread message is in the
    // window) and to anchor the entry scroll on a "New messages"
    // divider. The list is small and already cached server-side; a
    // missed lookup (deep-link to an unfamiliar thread, list call
    // 5xx) falls back to "no anchor, no unreads" gracefully.
    let unreadHint = 0
    let lastReadAt: string | null = null
    const summaryPromise = api.get('/api/conversations').then(
      (rows: Array<{ id: string; unread?: number; last_read_at?: string | null }>) => {
        const row = rows.find(r => r.id === convId)
        if (row) {
          unreadHint = Math.max(0, row.unread ?? 0)
          lastReadAt = row.last_read_at ?? null
        }
      },
    ).catch(() => {
      /* fall through with the defaults */
    })

    summaryPromise.then(() => {
      // Window size: enough to overflow the viewport comfortably (so
      // the user can actually scroll up and trigger ``loadOlder``),
      // but small enough that the entry skeleton-to-content swap
      // doesn't read as a long jump. 25 messages is the floor —
      // ~1600 px of content vs. a typical 600-800 px container,
      // giving a healthy ~1000 px of scroll-up headroom. Unread
      // spike widens further so the first-unread divider always
      // lands in the loaded set; capped at the backend's 100/request
      // ceiling so a very busy thread doesn't pay for a giant
      // payload on entry. The skeleton's ``justify-content: flex-end``
      // (see app.css) places its placeholder bubbles at the same
      // screen position as the real bottom-of-thread, so the swap
      // looks like a fade-in, not a scroll.
      const limit = Math.min(Math.max(unreadHint + 5, 25), 100)
      api.get(`/api/conversations/${convId}/messages?limit=${limit}`).then(data => {
        const msgs: Message[] = (data ?? []).slice().reverse()
        messages.value = msgs
        loading.value = false
        // If we got fewer than ``limit`` back, the thread is shorter
        // than the window — no older history to fetch.
        hasMoreHistory.value = (data ?? []).length === limit
        // Pick the first-unread message in the loaded window. A
        // message counts as unread when it was created strictly after
        // the caller's ``last_read_at`` AND was not authored by the
        // caller (a user's own message can't be "unread" to them).
        if (lastReadAt && unreadHint > 0) {
          const myId = currentUser.value?.user_id
          // ``normaliseTimestamp`` tags the naive SQLite shape
          // ("YYYY-MM-DD HH:MM:SS", no Z) as UTC before parsing —
          // without this, viewers in a non-UTC zone see V8 interpret
          // the naive string as *their* local wall clock and shift
          // ``lastReadMs`` by the UTC offset, landing the divider on
          // the wrong message (or no divider at all). The message's
          // own ``created_at`` is already tz-aware ISO but routing
          // both sides through the same helper keeps the math
          // consistent if that ever drifts.
          const lastReadMs = Date.parse(normaliseTimestamp(lastReadAt))
          if (!Number.isNaN(lastReadMs)) {
            const firstUnread = msgs.find(m =>
              m.sender_user_id !== myId
              && Date.parse(normaliseTimestamp(m.created_at)) > lastReadMs,
            )
            if (firstUnread) {
              unreadAnchor.value = { message_id: firstUnread.id }
            }
          }
        }
        // If there were no unreads (or no last_read_at), entry will
        // scroll to bottom — mark-as-read on entry stays unchanged for
        // that case. When there ARE unreads we defer the read POST
        // until the user actually scrolls past the divider (see the
        // ``handleScroll`` branch); marking on entry would advance the
        // watermark before the user has seen anything and the next
        // entry wouldn't render the divider.
        if (!unreadAnchor.value && readReceiptsEnabled.value) {
          api.post(`/api/conversations/${convId}/read`).catch(() => {})
        }
      }).catch(() => {
        // Network blip or 5xx — don't strand the user on the skeleton
        // forever. The thread page renders an empty list (which the
        // existing empty-state copy handles) and the next entry
        // retries; surfacing a toast would be louder than necessary
        // for a transient backend glitch.
        loading.value = false
        messages.value = []
        hasMoreHistory.value = false
      })
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
      if (data.conversation_id !== convId || !data.message) return
      const msg = data.message
      const mine = msg.sender_user_id === currentUser.value?.user_id
      // Strip any optimistic ``tmp-…`` row from the sender that's still
      // hanging around: the WS broadcast for our own send can race the
      // POST response back, and ``handleSend``'s id-swap only fires
      // once the response lands. Match on content (same sender + same
      // text) to avoid leaving the temp bubble next to the canonical
      // one. Other users' messages skip this branch entirely.
      let next = messages.value
      if (mine) {
        next = next.filter(m =>
          !(typeof m.id === 'string'
            && m.id.startsWith('tmp-')
            && m.content === msg.content),
        )
      }
      if (!next.some(m => m.id === msg.id)) {
        next = [...next, msg]
        if (!mine && readReceiptsEnabled.value) {
          // Ack delivery as soon as the frame lands. The server upsert
          // is idempotent; a later ``read`` supersedes.
          api.post(
            `/api/conversations/${convId}/messages/${msg.id}/delivered`,
          ).catch(() => {})
        }
      }
      if (next !== messages.value) messages.value = next
      // Only advance the watermark when the user is actually at the
      // bottom looking at the live edge — if they're scrolled up
      // reading historic context above the "New messages" divider,
      // the inbound message has NOT been seen yet, and marking it
      // as read here would defeat the deferred-read design (next
      // entry would see ``unread = 0`` and skip the divider).
      // ``stickToBottom`` is the same flag ``handleScroll`` maintains;
      // a self-send always satisfies it because ``handleSend`` flips
      // it to true before the optimistic append. The
      // sticky-bottom transition branch in ``handleScroll`` covers
      // the "user scrolls down to catch up" path.
      if (readReceiptsEnabled.value && stickToBottom.current) {
        api.post(`/api/conversations/${convId}/read`).catch(() => {})
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

  /** Grow the composer textarea to fit its content up to a hard cap,
   *  then scroll internally. Called on every input event + after send
   *  / reset to land the height back at one line. ``scrollHeight`` is
   *  the layout height required to show all content; assigning ``auto``
   *  first lets it shrink when the user deletes lines. */
  const autoResize = (el: HTMLTextAreaElement) => {
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, MAX_COMPOSER_HEIGHT_PX)}px`
  }

  /** Replace ``ta.value[start:end]`` with ``emoji`` and place the caret
   *  immediately after the inserted glyph. Shared by the
   *  ``:shortcode`` autocomplete (range = the typed token) and the
   *  ``EmojiPickButton`` (range = current caret position). */
  const spliceEmojiIntoTextarea = (emoji: string, range: [number, number]) => {
    const ta = composerInputRef.current
    if (!ta) return
    const [start, end] = range
    const before = ta.value.slice(0, start)
    const after = ta.value.slice(end)
    ta.value = before + emoji + after
    autoResize(ta)
    requestAnimationFrame(() => {
      if (!composerInputRef.current) return
      composerInputRef.current.focus()
      const pos = (before + emoji).length
      composerInputRef.current.setSelectionRange(pos, pos)
    })
  }

  /** Picker-button entry point — inserts at the current caret position. */
  const insertEmojiAtCursor = (emoji: string) => {
    const ta = composerInputRef.current
    if (!ta) return
    const pos = ta.selectionStart ?? ta.value.length
    spliceEmojiIntoTextarea(emoji, [pos, pos])
  }

  /** Slack-style ``:foo:`` → glyph: scan the textarea value for closed
   *  shortcode tokens (``:heart:``, ``:smile:``, …) and replace each
   *  one with the matching emoji glyph in place. Runs on every input
   *  event so the user gets immediate feedback the moment they type
   *  the closing colon. Returns the column-shift the caret should
   *  receive (number of characters removed before the caret). */
  const convertShortcodes = (ta: HTMLTextAreaElement) => {
    const before = ta.value
    const caret = ta.selectionStart ?? before.length
    let shiftBeforeCaret = 0
    const next = before.replace(
      /(^|[^a-zA-Z0-9_]):([a-zA-Z0-9_+-]+):/g,
      (match, lead: string, code: string, offset: number) => {
        const glyph = emojiByShortcode(code)
        if (!glyph) return match
        const removed = match.length - (lead.length + glyph.length)
        // Only the bytes BEFORE the caret shift its position. Tokens
        // that sit *after* the caret are still substituted but don't
        // move the caret.
        if (offset + match.length <= caret) shiftBeforeCaret += removed
        return lead + glyph
      },
    )
    if (next === before) return
    ta.value = next
    autoResize(ta)
    const newCaret = Math.max(0, caret - shiftBeforeCaret)
    ta.setSelectionRange(newCaret, newCaret)
  }

  const handleInput = (e: Event) => {
    const ta = e.currentTarget as HTMLTextAreaElement
    autoResize(ta)
    convertShortcodes(ta)
    // Slack-style ``:partial`` autocomplete — fires after the
    // close-colon substitution above so a fully-typed ``:heart:`` never
    // opens the dropdown (the glyph is already in place).
    checkForEmojiTrigger(
      ta.value,
      ta.selectionStart ?? 0,
      ta,
      spliceEmojiIntoTextarea,
    )
    if (typingTimer) return
    sendTyping(convId)
    typingTimer = setTimeout(() => { typingTimer = null }, 2000)
  }

  /** Send-on-Enter behaviour, branched by pointer kind:
   *
   *  - Desktop ``(pointer: fine)``: Enter submits, Shift+Enter inserts
   *    a newline. Same as Slack / Discord and WhatsApp Web.
   *  - Mobile ``(pointer: coarse)``: Enter inserts a newline, the user
   *    must tap the Send button to send. Matches WhatsApp on iOS /
   *    Android — the on-screen keyboard's Return key is for
   *    line-breaks, not for sending half-typed thoughts by accident.
   *
   *  IME composition (``isComposing``) bypasses the override entirely
   *  so Enter still confirms a Japanese / Chinese candidate the way
   *  the user expects.
   *
   *  When the ``:foo`` emoji autocomplete is open we hand the key off
   *  to it first so Enter / Tab / arrow keys drive the dropdown rather
   *  than the form submit.
   */
  const handleComposerKeyDown = (e: KeyboardEvent) => {
    if (handleEmojiAutocompleteKey(e)) {
      e.preventDefault()
      return
    }
    if (e.key !== 'Enter') return
    if (e.shiftKey) return  // explicit "give me a newline"
    if (e.isComposing) return  // IME — let the input swallow Enter
    const coarsePointer =
      typeof window !== 'undefined' &&
      window.matchMedia?.('(pointer: coarse)').matches
    if (coarsePointer) return  // touch → newline, send via button
    e.preventDefault()
    const ta = e.currentTarget as HTMLTextAreaElement
    ta.form?.requestSubmit()
  }

  const handleSend = async (e: Event) => {
    e.preventDefault()
    if (sending.value) return  // belt-and-braces guard for keyboard Enter
    const form = e.target as HTMLFormElement
    const content = (new FormData(form).get('content') as string ?? '').trim()
    if (!content) return
    const reply_to_id = replyTo.value?.id ?? null
    sending.value = true

    // **Optimistic append** — render the user's bubble immediately
    // instead of waiting for the POST round-trip + a full message-list
    // GET to repaint. The previous flow did two server round-trips
    // *and* a full re-render of every bubble before the Send spinner
    // stopped — on a busy thread that's hundreds of ms of dead time
    // staring at "Sending…". Now the bubble flashes in on click,
    // ``form.reset()`` clears the composer in the same frame, and the
    // POST resolves in the background. The canonical row (real id,
    // server timestamp) arrives via the WS broadcast a moment later
    // and de-dupes by ``id`` (see ``offNewMsg`` above — it also strips
    // any leftover ``tmp-`` row from the sender to avoid showing the
    // bubble twice if the WS frame races the POST response).
    const tempId = `tmp-${
      typeof crypto !== 'undefined' && crypto.randomUUID
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
    }`
    const myUid = currentUser.value?.user_id ?? ''
    const optimistic: Message = {
      id: tempId,
      sender_user_id: myUid,
      content,
      type: 'text',
      media_url: null,
      reply_to_id,
      deleted: false,
      created_at: new Date().toISOString(),
      edited_at: null,
    }
    messages.value = [...messages.value, optimistic]

    const draft = content
    form.reset()
    // ``form.reset()`` clears the value but leaves the explicit
    // ``style.height`` from a previous autoResize call, so the
    // composer would stay tall after sending a multi-line draft.
    // Wait one frame so the cleared value has settled through
    // layout, then re-run ``autoResize`` on the empty textarea —
    // its ``scrollHeight`` is now the natural one-line height and
    // the composer collapses back down. Without the rAF the
    // ``scrollHeight`` read still returns the pre-reset content's
    // dimensions and the bar stays inflated.
    const ta0 = composerInputRef.current
    if (ta0) {
      requestAnimationFrame(() => autoResize(ta0))
    }
    const restoredReply = replyTo.value
    replyTo.value = null
    try {
      const res = await api.post(`/api/conversations/${convId}/messages`, {
        content,
        ...(reply_to_id ? { reply_to_id } : {}),
      }) as { id: string }
      // Reconcile the optimistic row with the server-assigned id.
      //  • If the WS broadcast already landed (real ``id`` in the list)
      //    we just drop the temp.
      //  • Otherwise we swap the temp's id for the real one so a
      //    subsequent WS frame de-dupes naturally.
      const list = messages.value
      const realExists = list.some(m => m.id === res.id)
      messages.value = realExists
        ? list.filter(m => m.id !== tempId)
        : list.map(m => m.id === tempId ? { ...m, id: res.id } : m)
    } catch (err: unknown) {
      // Strip the optimistic row + restore the draft so the user
      // can retry without re-typing.
      messages.value = messages.value.filter(m => m.id !== tempId)
      const ta = (form.elements.namedItem('content') as HTMLTextAreaElement | null)
      if (ta) {
        ta.value = draft
        autoResize(ta)
        ta.focus()
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

  // Page title: show the peer's display name in the TopBar (above
  // the search box) so the user always knows whose chat they're in.
  // ``useTitle`` must run unconditionally on every render to keep the
  // rules-of-hooks invariant — placing it AFTER the ``loading.value``
  // early-return below would skip the hook on the loading frame and
  // crash. While the thread-member roster is still in flight we show
  // "Chats" as a placeholder so the topbar isn't briefly blank on
  // first entry.
  const peerTitle = (() => {
    const peers = threadMembers.value.filter(m => !m.is_self)
    if (peers.length === 0) return 'Chats'
    if (peers.length === 1) return peers[0].display_name
    // Group DM: join the peers with " · " — same shape the inbox
    // uses as the row-title fallback, so the topbar and the inbox
    // entry agree on what to call the conversation.
    return peers.map(p => p.display_name).join(' · ')
  })()
  useTitle(peerTitle)

  if (loading.value) return <DmThreadSkeleton />
  const myUserId = currentUser.value?.user_id

  const handleScroll = () => {
    const el = messagesScrollRef.current
    if (!el) return
    // Column-reverse coordinate system has a long-standing browser
    // split: Chrome / Safari / Edge use **negative** ``scrollTop``
    // values (0 at the visual bottom, ``-(scrollHeight -
    // clientHeight)`` at the visual top), while older Firefox
    // implementations used **positive** values mirroring the
    // non-reversed layout (0 at the visual top, max at the visual
    // bottom). Modern Firefox has moved to the Chrome convention,
    // but we normalise either way so a future user on a legacy
    // engine still gets correct sticky / lazy-load behaviour.
    //
    //   distFromBottom = magnitude away from the latest message.
    //                    0 at the visual bottom, ``maxScroll`` at
    //                    the visual top.
    //   distFromTop    = the complement.
    const maxScroll = Math.max(el.scrollHeight - el.clientHeight, 0)
    const distFromBottom =
      el.scrollTop <= 0
        ? -el.scrollTop                 // Chrome / modern Firefox
        : maxScroll - el.scrollTop      // legacy positive-scrollTop
    const distFromTop = maxScroll - distFromBottom
    const wasSticky = stickToBottom.current
    stickToBottom.current = distFromBottom < 80
    if (!wasSticky && stickToBottom.current) {
      // User returned to the bottom — clear the unread-since-scroll-up
      // counter so the CTA disappears and advance the read watermark
      // for any unread messages they've now caught up on. Gated on
      // actual pending state so an oscillating user (50 px up, 50 px
      // down) doesn't spam the endpoint on every false→true edge.
      const hadPending =
        unreadAnchor.value !== null || newSinceScrollUp.value > 0
      newSinceScrollUp.value = 0
      if (readReceiptsEnabled.value && hadPending) {
        api.post(`/api/conversations/${convId}/read`).catch(() => {})
      }
      // Clear the divider once the user has caught up.
      if (unreadAnchor.value) unreadAnchor.value = null
    }
    // Lazy-load older history when the user is within 120 px of the
    // visual top — far enough out that the fetch lands before the
    // user actually hits the very top. No "user has moved" gate is
    // needed: on entry ``distFromTop`` equals ``maxScroll``, which
    // is the maximum possible distance from the trigger — only a
    // real upward gesture can satisfy this condition.
    if (
      distFromTop < 120
      && hasMoreHistory.value
      && !isLoadingOlder.value
    ) {
      void loadOlder()
    }
  }

  /** Fetch the next page of older messages and prepend them.
   *
   *  ``column-reverse`` makes this dramatically simpler than the
   *  classic layout: the user's ``scrollTop`` is anchored relative
   *  to the **visual bottom**, and the prepended history lands at
   *  the **visual top** — i.e. on the opposite side of the
   *  scrollable region from the user's viewport reference. The
   *  user's reading position therefore stays exactly where it was,
   *  for free, with no snapshot / restore math.
   *
   *  ``isLoadingOlder`` gates re-entry so a touch-scroll dragging
   *  the user across the trigger threshold can't queue multiple
   *  parallel fetches. */
  const loadOlder = async () => {
    if (isLoadingOlder.value || !hasMoreHistory.value) return
    const oldest = messages.value[0]
    if (!oldest) return
    isLoadingOlder.value = true
    try {
      const data: Message[] = await api.get(
        `/api/conversations/${convId}/messages?before=${oldest.id}&limit=${PAGE_SIZE}`,
      ) ?? []
      const older = data.slice().reverse()
      if (older.length === 0) {
        hasMoreHistory.value = false
        return
      }
      // Deduplicate at the seam: if a slow re-entry surfaces the same
      // bottom-of-page message twice (rare; backend uses a strict
      // ``<`` filter on ``before``), keep only ids we don't have.
      const have = new Set(messages.value.map(m => m.id))
      const fresh = older.filter(m => !have.has(m.id))
      if (fresh.length === 0) {
        if (data.length < PAGE_SIZE) hasMoreHistory.value = false
        return
      }
      messages.value = [...fresh, ...messages.value]
      if (data.length < PAGE_SIZE) hasMoreHistory.value = false
    } finally {
      isLoadingOlder.value = false
    }
  }

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
        {/* Back chevron — visible on every viewport since the
         * full-bleed chat hides both the mobile bottom tab bar and
         * the desktop card outline. Users reach for an in-chat
         * back affordance regardless of screen size. */}
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
        {/* In-thread typing indicator — rendered FIRST in DOM so the
         *  ``column-reverse`` flex flip lands it at the visual
         *  BOTTOM of the messages area, right above the composer
         *  where the WhatsApp/iMessage-style bubble preview lives.
         *  Empty when nobody is typing — collapses with zero
         *  height impact on the scroll position. The ``bubble``
         *  prop opts into the bubble shape. */}
        <TypingIndicator scope={convId} bubble />
        {/* Floating "↓ N new messages" chip — appears in the
         *  bottom-right corner of the scrollable container when the
         *  user has scrolled up reading history and one or more new
         *  messages have arrived. Click jumps to the bottom and
         *  resets the counter via ``handleScroll``'s sticky-bottom
         *  branch. In column-reverse, "bottom" is ``scrollTop=0``.
         *  Rendered FIRST in DOM so the column-reverse flip places
         *  the sticky chip at the visual bottom. */}
        {newSinceScrollUp.value > 0 && !stickToBottom.current && (
          <button
            type="button"
            class="sh-dm-jump-down"
            onClick={() => {
              const el = messagesScrollRef.current
              if (!el) return
              stickToBottom.current = true
              el.scrollTo({ top: 0, behavior: 'smooth' })
              newSinceScrollUp.value = 0
              if (unreadAnchor.value) unreadAnchor.value = null
              if (readReceiptsEnabled.value) {
                api.post(`/api/conversations/${convId}/read`).catch(() => {})
              }
            }}
            aria-label={`Jump to latest, ${newSinceScrollUp.value} new ${
              newSinceScrollUp.value === 1 ? 'message' : 'messages'
            }`}
          >
            <span aria-hidden="true">↓</span>
            <span class="sh-dm-jump-down__count">
              {newSinceScrollUp.value > 99 ? '99+' : newSinceScrollUp.value}
            </span>
            <span class="sh-dm-jump-down__label">new</span>
          </button>
        )}
        {messages.value.slice().reverse().map(m => {
          // Render the "New messages" divider immediately above the
          // first-unread row visually. In column-reverse the
          // visually-above element is the one rendered AFTER the
          // anchor in DOM order, so the divider goes inside the
          // Fragment AFTER the message (not before).
          const isUnreadAnchor =
            unreadAnchor.value !== null
            && unreadAnchor.value.message_id === m.id
          if (m.type === 'call_event') {
            // Keyed Fragment for the outer slot so Preact's
            // reconciler moves the row correctly across prepend
            // updates (column-reverse means prepend lands at the
            // visual top, but the underlying DOM key matching is
            // still index-based without the Fragment key).
            return (
              <Fragment key={m.id}>
                <CallEventRow m={m} onCallBack={startCall} />
                {isUnreadAnchor && <UnreadDivider />}
              </Fragment>
            )
          }
          const mine = m.sender_user_id === myUserId
          // Look up the parent message for inline rendering of the
          // quoted-reply card. Missing parents (loaded out of window or
          // soft-deleted) fall through to a small placeholder.
          const parent = m.reply_to_id
            ? messages.value.find(x => x.id === m.reply_to_id)
            : null
          return (
            <Fragment key={m.id}>
            <div
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
            </Fragment>
          )
        })}
        {/* Lazy-load spinner — rendered LAST in DOM order so the
         *  ``column-reverse`` flex flip lands it at the visual TOP
         *  of the messages list, where the user is scrolling toward
         *  to fetch older history. Provides the "fetching older"
         *  affordance while the network round-trip is in flight. */}
        {isLoadingOlder.value && (
          <div class="sh-dm-load-older" aria-live="polite">Loading older…</div>
        )}
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
        <textarea
          ref={composerInputRef}
          name="content"
          placeholder={sending.value ? 'Sending…' : 'Type a message...'}
          autocomplete="off"
          rows={1}
          // Lock the input while the POST is in flight so a fast
          // typist can't keep adding to a message that's already
          // being delivered.
          disabled={sending.value}
          onInput={handleInput}
          onKeyDown={handleComposerKeyDown}
          onBlur={() => closeEmojiAutocomplete()}
        />
        {hasCapability('stt') && (
          <SttButton
            onText={(t) => {
              const input = composerInputRef.current
              if (!input) return
              const cur = input.value
              const sep = cur && !/\s$/.test(cur) ? ' ' : ''
              input.value = cur + sep + t
              autoResize(input)
              // Nudge the typing indicator + any input listeners.
              input.dispatchEvent(new Event('input', { bubbles: true }))
              input.focus()
            }}
          />
        )}
        <EmojiPickButton
          openKey="dm-composer"
          onInsert={insertEmojiAtCursor}
          ariaLabel="Insert emoji into message"
        />
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
      {/* Module-singleton popover for the ``:foo`` autocomplete the
       *  textarea triggers via ``checkForEmojiTrigger``. Mounting it
       *  inside the thread (rather than at app root) is fine — the
       *  popover positions itself absolutely against the input's
       *  bounding rect, not the parent. */}
      <EmojiAutocomplete />
    </div>
  )
}
