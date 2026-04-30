import { useEffect } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import { signal } from '@preact/signals'
import { api } from '@/api'
import type { Conversation } from '@/types'
import { Spinner } from '@/components/Spinner'
import { Button } from '@/components/Button'
import { openNewDm } from '@/components/NewDmDialog'
import { Avatar } from '@/components/Avatar'

const conversations = signal<Conversation[]>([])
const loading = signal(true)

export default function DmInboxPage() {
  useTitle('Messages')
  useEffect(() => {
    api.get('/api/conversations').then(data => {
      conversations.value = data
      loading.value = false
    }).catch(() => { loading.value = false })
  }, [])

  if (loading.value) return <Spinner />

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
      {conversations.value.map(c => (
        <a key={c.id} href={`/dms/${c.id}`} class="sh-dm-row">
          <Avatar name={c.name || 'DM'} size={40} />
          <div class="sh-dm-info">
            <strong>{c.name || 'Direct message'}</strong>
            <span class="sh-badge">{c.type === 'group_dm' ? 'Group' : 'DM'}</span>
          </div>
          {c.last_message_at && (
            <time class="sh-muted">{new Date(c.last_message_at).toLocaleString()}</time>
          )}
        </a>
      ))}
    </div>
  )
}
