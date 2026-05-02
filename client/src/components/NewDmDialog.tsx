/**
 * NewDmDialog — start a new DM or group DM (§23.47b).
 *
 * UX shape:
 *   • Search input filters the household list.
 *   • Click a row to toggle membership; selected users appear as
 *     removable chips above the list. Empty selection disables Start.
 *   • One member selected → POST /api/conversations/dm (1:1).
 *   • Two or more selected → POST /api/conversations/group; an
 *     optional "Group name" input slides in so the conversation reads
 *     as something specific in everyone's inbox ("Sunday lunch crew"
 *     beats "Direct message" for a 4-person thread).
 *   • Backend enforces the 3-participants minimum (creator + ≥2
 *     others) on `/group`; we mirror that gate client-side so the
 *     button stays meaningful.
 *
 * On successful create the dialog navigates straight into the new
 * thread — "create" and "open" feel like one action, not two.
 */
import { signal, computed } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { currentUser } from '@/store/auth'
import { Avatar } from './Avatar'
import { Modal } from './Modal'
import { Button } from './Button'
import { showToast } from './Toast'
import type { User } from '@/types'

const open       = signal(false)
const users      = signal<User[]>([])
/** Set of usernames selected for the new conversation. Empty until
 *  the user picks at least one. The size of this set drives the
 *  one-vs-group endpoint routing on submit. */
const picked     = signal<Set<string>>(new Set())
const groupName  = signal('')
const search     = signal('')
const loading    = signal(false)

const recipients = computed<User[]>(() => {
  const me = currentUser.value?.username
  return users.value.filter((u) => u.username !== me)
})

const filtered = computed<User[]>(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return recipients.value
  return recipients.value.filter(u =>
    u.username.toLowerCase().includes(q)
    || u.display_name.toLowerCase().includes(q),
  )
})

const pickedUsers = computed<User[]>(() => {
  const set = picked.value
  return recipients.value.filter(u => set.has(u.username))
})

const isGroup = computed(() => picked.value.size >= 2)

function reset() {
  picked.value = new Set()
  groupName.value = ''
  search.value = ''
}

function togglePick(username: string) {
  const next = new Set(picked.value)
  if (next.has(username)) {
    next.delete(username)
  } else {
    next.add(username)
  }
  picked.value = next
}

export function openNewDm() {
  reset()
  open.value = true
  api.get('/api/users').then(data => { users.value = data })
}

export function NewDmDialog({ onCreated }: { onCreated?: (convId: string) => void }) {
  const location = useLocation()

  const handleCreate = async () => {
    if (loading.value) return
    const list = Array.from(picked.value)
    if (list.length === 0) return
    loading.value = true
    try {
      let conv: { id: string }
      if (list.length === 1) {
        conv = await api.post('/api/conversations/dm', { username: list[0] })
        showToast('Conversation started', 'success')
      } else {
        const body: { members: string[]; name?: string } = { members: list }
        const trimmedName = groupName.value.trim()
        if (trimmedName) body.name = trimmedName
        conv = await api.post('/api/conversations/group', body)
        showToast(
          trimmedName ? `Group "${trimmedName}" created` : 'Group created',
          'success',
        )
      }
      open.value = false
      reset()
      if (onCreated) {
        onCreated(conv.id)
      } else {
        location.route(`/dms/${conv.id}`)
      }
    } catch (e: any) {
      showToast(e.message || 'Failed to start conversation', 'error')
    } finally {
      loading.value = false
    }
  }

  const startDisabled = loading.value || picked.value.size === 0

  return (
    <Modal
      open={open.value}
      onClose={() => { open.value = false; reset() }}
      title={isGroup.value ? 'New group message' : 'New message'}
    >
      <div class="sh-form sh-newdm">
        {/* Selected-chip strip — only renders when at least one is
            picked, so the dialog stays compact for quick 1:1s. */}
        {picked.value.size > 0 && (
          <div class="sh-newdm-chips" aria-label="Selected recipients">
            {pickedUsers.value.map(u => (
              <button
                key={u.username}
                type="button"
                class="sh-newdm-chip"
                aria-label={`Remove ${u.display_name}`}
                onClick={() => togglePick(u.username)}
              >
                <Avatar name={u.display_name} size={20} />
                <span class="sh-newdm-chip-name">{u.display_name}</span>
                <span class="sh-newdm-chip-x" aria-hidden="true">×</span>
              </button>
            ))}
          </div>
        )}

        {/* Group-name field appears only when the conversation will
            actually be a group. Optional — backend allows null. */}
        {isGroup.value && (
          <label class="sh-newdm-name">
            Group name <span class="sh-muted">(optional)</span>
            <input
              type="text"
              maxLength={80}
              value={groupName.value}
              onInput={(e) => {
                groupName.value = (e.target as HTMLInputElement).value
              }}
              placeholder="e.g. Sunday lunch crew"
            />
          </label>
        )}

        <label class="sh-newdm-search">
          {picked.value.size === 0 ? 'To:' : 'Add more:'}
          <input
            type="search"
            placeholder="Search household members…"
            value={search.value}
            onInput={(e) => {
              search.value = (e.target as HTMLInputElement).value
            }}
            autoFocus
          />
        </label>

        <div class="sh-newdm-userlist" role="listbox" aria-multiselectable="true">
          {filtered.value.length === 0 && (
            <p class="sh-muted" style={{ padding: 'var(--sh-space-sm)' }}>
              {search.value
                ? 'No matches.'
                : 'No other household members yet.'}
            </p>
          )}
          {filtered.value.map(u => {
            const checked = picked.value.has(u.username)
            return (
              <button
                key={u.username}
                type="button"
                role="option"
                aria-selected={checked}
                class={
                  checked
                    ? 'sh-newdm-row sh-newdm-row--checked'
                    : 'sh-newdm-row'
                }
                onClick={() => togglePick(u.username)}
              >
                <Avatar name={u.display_name} size={32} />
                <div class="sh-newdm-row-meta">
                  <strong>{u.display_name}</strong>
                  <span class="sh-muted">@{u.username}</span>
                </div>
                <span class="sh-newdm-check" aria-hidden="true">
                  {checked ? '✓' : ''}
                </span>
              </button>
            )
          })}
        </div>

        <div class="sh-form-actions">
          <Button
            variant="secondary"
            onClick={() => { open.value = false; reset() }}
          >
            Cancel
          </Button>
          <Button
            onClick={handleCreate}
            loading={loading.value}
            disabled={startDisabled}
          >
            {isGroup.value
              ? `Start group (${picked.value.size})`
              : 'Start'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
