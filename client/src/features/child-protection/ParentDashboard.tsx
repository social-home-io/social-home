/**
 * ParentDashboard — guardian monitoring view (spec §23.104).
 *
 * Pulls the caller's assigned minors from ``GET /api/cp/minors`` and
 * renders, per minor, the block list + a collapsible audit-log viewer.
 * Listens for CP WS events so block changes from another session
 * refresh the UI live.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import type { User } from '@/types'
import { GuardianAuditLog } from './GuardianAuditLog'
import { confirmDialog } from '@/components/confirm'

interface BlockRow {
  blocked_user_id: string
  blocked_by:      string
  blocked_at:      string
}

interface SpaceRow {
  id:         string
  name:       string
  emoji?:     string | null
  space_type: string
}

interface ConversationRow {
  id:              string
  type:            string
  name:            string | null
  last_message_at: string | null
}

interface DmContactRow {
  username:        string
  conversation_id: string
}

type SectionKey = 'blocks' | 'spaces' | 'conversations' | 'contacts'

interface MinorBundle {
  user_id: string
  display_name: string
  username: string
  blocks: BlockRow[]
  spaces: SpaceRow[]
  conversations: ConversationRow[]
  contacts: DmContactRow[]
  /** Per-section load failures. Each section that failed renders an
   *  error chip with Retry instead of an empty list — guardians could
   *  otherwise act on a silently-incomplete picture of the minor. */
  errors: Partial<Record<SectionKey, boolean>>
}

/** Section-scoped fetchers. Indexed by ``SectionKey`` so the retry
 *  helper can call exactly the one that failed without re-running the
 *  rest. */
const SECTION_FETCHERS: Record<
  SectionKey,
  (id: string) => Promise<Partial<MinorBundle>>
> = {
  async blocks(id) {
    const r = await api.get(
      `/api/cp/minors/${id}/blocks`,
    ) as { blocks: BlockRow[] }
    return { blocks: r.blocks }
  },
  async spaces(id) {
    const r = await api.get(
      `/api/cp/minors/${id}/spaces`,
    ) as { spaces: SpaceRow[] }
    return { spaces: r.spaces }
  },
  async conversations(id) {
    const r = await api.get(
      `/api/cp/minors/${id}/conversations`,
    ) as { conversations: ConversationRow[] }
    return { conversations: r.conversations }
  },
  async contacts(id) {
    const r = await api.get(
      `/api/cp/minors/${id}/dm-contacts`,
    ) as { contacts: DmContactRow[] }
    return { contacts: r.contacts }
  },
}

async function retrySection(minorId: string, section: SectionKey): Promise<void> {
  try {
    const patch = await SECTION_FETCHERS[section](minorId)
    minors.value = minors.value.map(m =>
      m.user_id === minorId
        ? { ...m, ...patch, errors: { ...m.errors, [section]: false } }
        : m,
    )
  } catch (err: unknown) {
    showToast(
      `Retry failed: ${(err as Error)?.message ?? err}`, 'error',
    )
  }
}

const minors    = signal<MinorBundle[]>([])
const loading   = signal(true)
const openLog   = signal<string | null>(null)

async function loadMinors(): Promise<void> {
  loading.value = true
  try {
    // 1. Fetch ids of minors the caller guards.
    const ids = (await api.get('/api/cp/minors') as { minors: string[] }).minors
    if (ids.length === 0) {
      minors.value = []
      return
    }
    // 2. Fetch the household user directory so we can resolve display
    //    names for each id. The API returns local users only, which is
    //    the set the parent dashboard cares about.
    const users = await api.get('/api/users') as User[]
    const byId = new Map(users.map(u => [u.user_id, u]))
    // 3. For each minor, load their block list + joined spaces in
    //    parallel. Each section is independent — a 5xx on /spaces
    //    must not silently empty out /blocks. Track which sections
    //    failed so the render can show a Retry chip per section
    //    instead of acting on partial data.
    const bundles = await Promise.all(ids.map(async id => {
      const u = byId.get(id)
      const errors: Partial<Record<SectionKey, boolean>> = {}
      const blocks: BlockRow[] = []
      const spaces: SpaceRow[] = []
      const conversations: ConversationRow[] = []
      const contacts: DmContactRow[] = []
      const sections: SectionKey[] = ['blocks', 'spaces', 'conversations', 'contacts']
      const data: Record<SectionKey, unknown[]> = {
        blocks, spaces, conversations, contacts,
      }
      await Promise.all(sections.map(async section => {
        try {
          const patch = await SECTION_FETCHERS[section](id)
          const rows = (patch[section] ?? []) as unknown[]
          data[section].push(...rows)
        } catch {
          errors[section] = true
        }
      }))
      return {
        user_id:      id,
        display_name: u?.display_name ?? id,
        username:     u?.username ?? id,
        blocks,
        spaces,
        conversations,
        contacts,
        errors,
      }
    }))
    minors.value = bundles
  } catch (e: unknown) {
    showToast((e as Error).message || 'Could not load dashboard', 'error')
    minors.value = []
  } finally {
    loading.value = false
  }
}

async function unblock(minorUserId: string, blockedUserId: string) {
  try {
    await api.delete(`/api/cp/minors/${minorUserId}/blocks/${blockedUserId}`)
    showToast('Block removed', 'info')
    void loadMinors()
  } catch (e: unknown) {
    showToast((e as Error).message || 'Unblock failed', 'error')
  }
}

async function kickFromSpace(minorUserId: string, spaceId: string, spaceName: string) {
  if (!await confirmDialog(`Kick this minor from "${spaceName}"? They can be re-added later.`, { destructive: true })) {
    return
  }
  try {
    await api.post(
      `/api/cp/minors/${minorUserId}/spaces/${spaceId}/kick`, {},
    )
    showToast(`Removed from ${spaceName}`, 'success')
    void loadMinors()
  } catch (e: unknown) {
    showToast((e as Error).message || 'Kick failed', 'error')
  }
}

export default function ParentDashboard() {
  useEffect(() => {
    void loadMinors()
    const off1 = ws.on('cp.block_added',   () => { void loadMinors() })
    const off2 = ws.on('cp.block_removed', () => { void loadMinors() })
    const off3 = ws.on('cp.guardian_added',   () => { void loadMinors() })
    const off4 = ws.on('cp.guardian_removed', () => { void loadMinors() })
    const off5 = ws.on('space.member.joined', () => { void loadMinors() })
    const off6 = ws.on('space.member.left',   () => { void loadMinors() })
    return () => { off1(); off2(); off3(); off4(); off5(); off6() }
  }, [])

  if (loading.value) return <Spinner />

  return (
    <div class="sh-parent-dashboard">
      <h2>Parent dashboard</h2>
      <p class="sh-muted">Monitor activity for minors in your household.</p>

      {minors.value.length === 0 ? (
        <div class="sh-empty-state">
          <p>No minors assigned to your guardianship.</p>
          <p class="sh-muted">
            Ask a household admin to assign you as a guardian.
          </p>
        </div>
      ) : (
        <div class="sh-minor-cards">
          {minors.value.map(m => (
            <div key={m.user_id} class="sh-minor-card sh-card">
              <header class="sh-row">
                <Avatar name={m.display_name} size={48} />
                <div class="sh-minor-info">
                  <strong>{m.display_name}</strong>
                  <span class="sh-muted">@{m.username}</span>
                </div>
              </header>

              <section class="sh-cp-blocks">
                <strong>Blocked users</strong>
                {m.errors.blocks ? (
                  <SectionError minorId={m.user_id} section="blocks" />
                ) : m.blocks.length === 0 ? (
                  <p class="sh-muted">No blocks set.</p>
                ) : (
                  <ul>
                    {m.blocks.map(b => (
                      <li key={b.blocked_user_id} class="sh-row">
                        <span>{b.blocked_user_id}</span>
                        <span class="sh-muted">
                          since {new Date(b.blocked_at).toLocaleDateString()}
                        </span>
                        <Button variant="secondary"
                                onClick={() => unblock(m.user_id, b.blocked_user_id)}>
                          Unblock
                        </Button>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              <section class="sh-cp-spaces">
                <strong>Joined spaces</strong>
                {m.errors.spaces ? (
                  <SectionError minorId={m.user_id} section="spaces" />
                ) : m.spaces.length === 0 ? (
                  <p class="sh-muted">Not in any space.</p>
                ) : (
                  <ul>
                    {m.spaces.map(s => (
                      <li key={s.id} class="sh-row">
                        <span>{s.emoji} {s.name}</span>
                        <span class="sh-muted">{s.space_type}</span>
                        <Button variant="danger"
                                onClick={() =>
                                  kickFromSpace(m.user_id, s.id, s.name)}>
                          Kick
                        </Button>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              <section class="sh-cp-convs">
                <strong>Active conversations</strong>
                {m.errors.conversations ? (
                  <SectionError minorId={m.user_id} section="conversations" />
                ) : m.conversations.length === 0 ? (
                  <p class="sh-muted">Not in any conversation.</p>
                ) : (
                  <ul>
                    {m.conversations.map(c => (
                      <li key={c.id} class="sh-row">
                        <span>{c.name || (c.type === 'dm' ? 'Direct message' : 'Group')}</span>
                        <span class="sh-muted">
                          {c.last_message_at
                            ? new Date(c.last_message_at).toLocaleString()
                            : '—'}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              <section class="sh-cp-contacts">
                <strong>Chats with</strong>
                {m.errors.contacts ? (
                  <SectionError minorId={m.user_id} section="contacts" />
                ) : m.contacts.length === 0 ? (
                  <p class="sh-muted">Not messaging anyone.</p>
                ) : (
                  <ul>
                    {m.contacts.map(c => (
                      <li key={c.username + c.conversation_id} class="sh-row">
                        <span>@{c.username}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              <Button
                variant="secondary"
                onClick={() => openLog.value = openLog.value === m.user_id ? null : m.user_id}>
                {openLog.value === m.user_id ? 'Hide audit log' : 'Audit log'}
              </Button>
              {openLog.value === m.user_id && (
                <GuardianAuditLog minorId={m.user_id} />
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function SectionError({
  minorId, section,
}: { minorId: string; section: SectionKey }) {
  return (
    <div class="sh-error" role="alert" style={{ marginTop: '0.25rem' }}>
      <p class="sh-muted" style={{ margin: 0 }}>
        Couldn't load this section.
      </p>
      <Button
        variant="secondary"
        onClick={() => void retrySection(minorId, section)}
      >
        Retry
      </Button>
    </div>
  )
}
