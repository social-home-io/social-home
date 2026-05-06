/**
 * DMs store — driven by `dm.message`, `dm.message_deleted`,
 * `dm.message_reaction` and `conversation.user_typing` WS frames.
 *
 * DmInboxPage reads :data:`inbox` (latest message per conversation).
 * DmThreadPage reads :data:`messagesByConversation[conversationId]`
 * and :data:`typingByConversation[conversationId]` — both update
 * without manual polling thanks to the WS subscription wired below.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'

export interface DmMessageLite {
  message_id:      string
  conversation_id: string
  sender_user_id:  string
  sender_display?: string
  content:         string
  occurred_at?:    string
  edited_at?:      string | null
}

export interface DmReactionPatch {
  message_id:      string
  conversation_id: string
  emoji:           string
  user_id:         string
  action:          'add' | 'remove'
}

export interface TypingIndicator {
  conversation_id: string
  user_id:         string
  until:           number
}

export const inbox = signal<Record<string, DmMessageLite>>({})
export const messagesByConversation = signal<Record<string, DmMessageLite[]>>({})
export const typingByConversation = signal<Record<string, TypingIndicator[]>>({})

/** Total unread DMs across every conversation the user is a member
 *  of. Refreshed by :class:`DmInboxPage` after each
 *  ``GET /api/conversations`` (initial fetch + post-WS refetches),
 *  consumed by the sidebar to render the Chats badge. Stays a plain
 *  signal — derived state lives at the producer's boundary so a
 *  reload page that never mounts the inbox still sees zero, never a
 *  stale count. */
export const dmUnreadTotal = signal<number>(0)

function append(convo: string, msg: DmMessageLite): void {
  const existing = messagesByConversation.value[convo] ?? []
  if (existing.some((m) => m.message_id === msg.message_id)) return
  messagesByConversation.value = {
    ...messagesByConversation.value,
    [convo]: [...existing, msg],
  }
  inbox.value = { ...inbox.value, [convo]: msg }
}

function removeMessage(convo: string, messageId: string): void {
  const existing = messagesByConversation.value[convo]
  if (!existing) return
  messagesByConversation.value = {
    ...messagesByConversation.value,
    [convo]: existing.filter((m) => m.message_id !== messageId),
  }
}

function addTyping(convo: string, userId: string, ttlSeconds = 6): void {
  const until = Date.now() + ttlSeconds * 1000
  const existing = (typingByConversation.value[convo] ?? []).filter(
    (t) => t.user_id !== userId && t.until > Date.now(),
  )
  typingByConversation.value = {
    ...typingByConversation.value,
    [convo]: [...existing, { conversation_id: convo, user_id: userId, until }],
  }
}

export async function loadDmUnread(): Promise<void> {
  try {
    const rows = (await api.get('/api/conversations')) as Array<{ unread?: number }>
    let sum = 0
    for (const r of rows ?? []) sum += Math.max(0, r.unread ?? 0)
    dmUnreadTotal.value = sum
  } catch {
    /* auth not ready or transient — leave the prior count visible */
  }
}

export function wireDmWs(): void {
  ws.on('dm.message', (e) => {
    const m = e.data as unknown as DmMessageLite
    if (!m?.conversation_id || !m?.message_id) return
    append(m.conversation_id, m)
    void loadDmUnread()
  })
  ws.on('dm.message_deleted', (e) => {
    const d = e.data as unknown as { conversation_id: string, message_id: string }
    if (!d?.conversation_id || !d?.message_id) return
    removeMessage(d.conversation_id, d.message_id)
    void loadDmUnread()
  })
  ws.on('dm.message_reaction', (e) => {
    const r = e.data as unknown as DmReactionPatch
    const convoMessages = messagesByConversation.value[r.conversation_id]
    if (!convoMessages) return
    messagesByConversation.value = {
      ...messagesByConversation.value,
      [r.conversation_id]: [...convoMessages],
    }
  })
  ws.on('conversation.user_typing', (e) => {
    const t = e.data as unknown as { conversation_id: string, user_id: string, ttl?: number }
    if (!t?.conversation_id || !t?.user_id) return
    addTyping(t.conversation_id, t.user_id, t.ttl ?? 6)
  })
}
