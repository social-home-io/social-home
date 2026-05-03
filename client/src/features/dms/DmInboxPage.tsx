import { useEffect } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import type { Conversation } from '@/types'
import { DmInboxSkeleton } from '@/components/Skeleton'
import { Button } from '@/components/Button'
import { openNewDm } from '@/components/NewDmDialog'
import { Avatar } from '@/components/Avatar'

const conversations = signal<Conversation[]>([])
const loading = signal(true)

const reload = () =>
  api.get('/api/conversations').then(data => {
    conversations.value = data
  }).catch(() => { /* noop — keep current list on transient failures */ })

export default function DmInboxPage() {
  useTitle('Messages')
  useEffect(() => {
    void reload().finally(() => { loading.value = false })
    // Refresh on any DM frame the server fans out — new conversations
    // and new messages both bump ``last_message_at`` ordering.
    const offMsg  = ws.on('dm.message',              () => { void reload() })
    const offConv = ws.on('dm.conversation.created', () => { void reload() })
    return () => { offMsg(); offConv() }
  }, [])

  if (loading.value) return <DmInboxSkeleton />

  return (
    <div class="sh-dms">
      <div class="sh-page-header">
        <Button onClick={() => openNewDm()}>+ New message</Button>
      </div>
      {conversations.value.length === 0 && (
        <div class="sh-empty-state">
          <p>No conversations yet.</p>
          <p class="sh-muted">Start a conversation with someone in your household.</p>
        </div>
      )}
      {conversations.value.map(c => {
        const peers = c.members ?? []
        // Display-name fallback: explicit name > peer display names
        // (joined by "·") > "Direct message". The peer-name path is
        // what most groups end up with — even unnamed ones read as
        // "Anna · Bob · Carol" instead of an opaque "Direct message".
        const fallbackName = peers.length > 0
          ? peers.map(p => p.display_name).join(' · ')
          : 'Direct message'
        const displayName = c.name || fallbackName
        // Stack up to 3 avatars, then a "+N" overflow chip. Falls
        // back to a single avatar from the conversation name when
        // members are missing (legacy rows from before this field).
        const visiblePeers = peers.slice(0, 3)
        const overflow = peers.length - visiblePeers.length
        return (
          <a key={c.id} href={`/dms/${c.id}`} class="sh-dm-row">
            <div class="sh-dm-avatars">
              {peers.length === 0 ? (
                <Avatar name={displayName} size={40} />
              ) : (
                <>
                  {visiblePeers.map(p => (
                    <Avatar
                      key={p.user_id}
                      name={p.display_name}
                      src={p.picture_url}
                      size={32}
                    />
                  ))}
                  {overflow > 0 && (
                    <span class="sh-dm-avatar-more">+{overflow}</span>
                  )}
                </>
              )}
            </div>
            <div class="sh-dm-info">
              <strong>{displayName}</strong>
              <span class="sh-badge">
                {c.type === 'group_dm'
                  ? `Group · ${c.member_count ?? peers.length + 1}`
                  : 'DM'}
              </span>
            </div>
            {c.last_message_at && (
              <time class="sh-muted">{new Date(c.last_message_at).toLocaleString()}</time>
            )}
          </a>
        )
      })}
    </div>
  )
}
