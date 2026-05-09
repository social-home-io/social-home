/**
 * TaskPage — multi-list task manager (§23.54 / §15).
 *
 * Sidebar holds the list roster with create + rename + delete. Main
 * pane shows the active list's tasks with inline-add, a status
 * segmented control (todo / in-progress / done), due-date pill, edit
 * dialog, and status changes via the central store so WS frames from
 * other tabs merge automatically.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { TasksSkeleton } from '@/components/Skeleton'
import { showToast } from '@/components/Toast'
import { useTitle } from '@/store/pageTitle'
import { lists, tasks } from '@/store/tasks'
import { householdUsers, loadHouseholdUsers } from '@/store/householdUsers'
import { currentUser } from '@/store/auth'
import type { TaskItem, TaskListEntry } from '@/types'
import { confirmDialog } from '@/components/confirm'

const activeList = signal<string | null>(null)
const loading = signal(true)
const editingTask = signal<TaskItem | null>(null)

const newListName = signal('')
const newTaskTitle = signal('')

type Status = 'todo' | 'in_progress' | 'done'
const STATUS_LABEL: Record<Status, string> = {
  todo:        'To do',
  in_progress: 'In progress',
  done:        'Done',
}
const STATUS_CYCLE: Record<Status, Status> = {
  todo:        'in_progress',
  in_progress: 'done',
  done:        'todo',
}

function dueLabel(due: string): { text: string; modifier: 'due' | 'overdue' | null } {
  // Parse a YYYY-MM-DD or ISO timestamp; reduce to a date-only comparison
  // against "today" so a task due today doesn't read as "overdue" until
  // tomorrow rolls over.
  const d = new Date(due)
  if (Number.isNaN(d.getTime())) return { text: due, modifier: null }
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const dueDay = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const ms = dueDay.getTime() - today.getTime()
  const days = Math.round(ms / 86400000)
  if (days < 0) return { text: 'overdue', modifier: 'overdue' }
  if (days === 0) return { text: 'today', modifier: 'due' }
  if (days === 1) return { text: 'tomorrow', modifier: 'due' }
  if (days <= 7) return { text: 'this week', modifier: 'due' }
  return { text: due, modifier: null }
}

export default function TaskPage() {
  useEffect(() => {
    void loadHouseholdUsers()
    void (async () => {
      try {
        const rows = await api.get('/api/tasks/lists') as TaskListEntry[]
        lists.value = rows
        if (rows.length > 0 && !activeList.value) {
          activeList.value = rows[0].id
          await loadTasks(rows[0].id)
        }
      } finally {
        loading.value = false
      }
    })()
  }, [])

  const loadTasks = async (listId: string) => {
    activeList.value = listId
    try {
      const rows = await api.get(
        `/api/tasks/lists/${listId}/tasks`,
      ) as TaskItem[]
      // Keep any tasks from other lists already in the store (e.g. from
      // a WS frame); replace the ones for this list with the fresh REST
      // response so it's the source of truth for what we just loaded.
      const other = tasks.value.filter(t => t.list_id !== listId)
      tasks.value = [...other, ...rows]
    } catch (err: unknown) {
      showToast(`Could not load tasks: ${(err as Error).message ?? err}`, 'error')
    }
  }

  const submitList = async (e: Event) => {
    e.preventDefault()
    const name = newListName.value.trim()
    if (!name) return
    try {
      const list = await api.post(
        '/api/tasks/lists', { name },
      ) as TaskListEntry
      if (!lists.value.some(l => l.id === list.id)) {
        lists.value = [...lists.value, list]
      }
      activeList.value = list.id
      newListName.value = ''
    } catch (err: unknown) {
      showToast(`Create list failed: ${(err as Error).message ?? err}`, 'error')
    }
  }

  const renameList = async (list: TaskListEntry, nextName: string) => {
    const trimmed = nextName.trim()
    if (!trimmed || trimmed === list.name) return
    try {
      const updated = await api.patch(
        `/api/tasks/lists/${list.id}`, { name: trimmed },
      ) as TaskListEntry
      lists.value = lists.value.map(l => l.id === list.id ? updated : l)
    } catch (err: unknown) {
      showToast(`Rename failed: ${(err as Error).message ?? err}`, 'error')
    }
  }

  const deleteList = async (list: TaskListEntry) => {
    if (!await confirmDialog(
      `Delete "${list.name}" and all its tasks? This can't be undone.`, { destructive: true })) return
    try {
      await api.delete(`/api/tasks/lists/${list.id}`)
      lists.value = lists.value.filter(l => l.id !== list.id)
      tasks.value = tasks.value.filter(t => t.list_id !== list.id)
      if (activeList.value === list.id) {
        activeList.value = lists.value[0]?.id ?? null
        if (activeList.value) void loadTasks(activeList.value)
      }
      showToast(`Deleted "${list.name}"`, 'info')
    } catch (err: unknown) {
      showToast(`Delete failed: ${(err as Error).message ?? err}`, 'error')
    }
  }

  const submitTask = async (e: Event) => {
    e.preventDefault()
    if (!activeList.value) return
    const title = newTaskTitle.value.trim()
    if (!title) return
    try {
      const task = await api.post(
        `/api/tasks/lists/${activeList.value}/tasks`, { title },
      ) as TaskItem
      if (!tasks.value.some(t => t.id === task.id)) {
        tasks.value = [...tasks.value, task]
      }
      newTaskTitle.value = ''
    } catch (err: unknown) {
      showToast(`Add task failed: ${(err as Error).message ?? err}`, 'error')
    }
  }

  const cycleStatus = async (task: TaskItem) => {
    const next = STATUS_CYCLE[task.status as Status]
    try {
      const updated = await api.patch(
        `/api/tasks/${task.id}`, { status: next },
      ) as TaskItem
      tasks.value = tasks.value.map(t => t.id === task.id ? updated : t)
    } catch (err: unknown) {
      showToast(`Update failed: ${(err as Error).message ?? err}`, 'error')
    }
  }

  const deleteTask = async (task: TaskItem) => {
    if (!await confirmDialog(`Delete "${task.title}"?`, { destructive: true })) return
    try {
      await api.delete(`/api/tasks/${task.id}`)
      tasks.value = tasks.value.filter(t => t.id !== task.id)
      showToast('Task deleted', 'info')
    } catch (err: unknown) {
      showToast(`Delete failed: ${(err as Error).message ?? err}`, 'error')
    }
  }

  if (loading.value) return <TasksSkeleton />

  const me = currentUser.value
  const userNameById = (uid: string): string => {
    if (me?.user_id === uid) return 'you'
    const found = householdUsers.value.get(uid)
    return found?.display_name || found?.username || uid.slice(0, 6)
  }

  const visibleTasks = tasks.value
    .filter(t => t.list_id === activeList.value)
    .sort((a, b) => (a.position ?? 0) - (b.position ?? 0))

  const activeListEntry = lists.value.find(l => l.id === activeList.value)
  // Reactive page title — flips when the user picks a different list.
  useTitle(activeListEntry?.name || 'Tasks')

  // Count of open tasks across every list — gives the sidebar header
  // a real "you have N things on your plate" stat.  Tasks store
  // currently keeps the active-list rows; the count is approximate
  // for lists not yet visited (we don't paginate-eagerly), but the
  // active list always shows its real total.
  const totalOpen = tasks.value.filter(t => t.status !== 'done').length
  const newListInputRef = useRef<HTMLInputElement | null>(null)

  return (
    <div class="sh-tasks">
      <aside class="sh-tasks-sidebar">
        <header class="sh-tasks-sidebar-header">
          <h3>Lists</h3>
          <span class="sh-muted">
            {lists.value.length === 1
              ? '1 list'
              : `${lists.value.length} lists`}
            {lists.value.length > 0 && totalOpen > 0 && (
              <> · {totalOpen} open</>
            )}
          </span>
        </header>
        {lists.value.map(l => (
          <ListRow
            key={l.id} list={l}
            active={activeList.value === l.id}
            onSelect={() => void loadTasks(l.id)}
            onRename={(name) => void renameList(l, name)}
            onDelete={() => void deleteList(l)}
          />
        ))}
        <form class="sh-form-row" onSubmit={submitList}
              style={{ marginTop: '0.5rem' }}>
          <input
            ref={newListInputRef}
            type="text"
            value={newListName.value}
            placeholder="+ New list"
            onInput={(e) => newListName.value = (e.target as HTMLInputElement).value}
            aria-label="New list name"
          />
        </form>
      </aside>

      <div class="sh-tasks-content">
        {activeListEntry && (
          <div class="sh-page-header sh-tasks-page-header">
            <h2 class="sh-tasks-page-title">{activeListEntry.name}</h2>
            <span class="sh-tasks-page-counts">
              <span class="sh-tasks-page-counts__open">
                {visibleTasks.filter(t => t.status !== 'done').length} open
              </span>
              <span class="sh-tasks-page-counts__sep" aria-hidden="true">·</span>
              <span class="sh-tasks-page-counts__done">
                {visibleTasks.filter(t => t.status === 'done').length} done
              </span>
            </span>
          </div>
        )}

        {activeList.value && (
          <form class="sh-form-row sh-composer" onSubmit={submitTask}
                style={{ marginBottom: 0, padding: '0.5rem 0.75rem' }}>
            <input
              type="text"
              value={newTaskTitle.value}
              placeholder="Add a task and press Enter…"
              onInput={(e) => newTaskTitle.value = (e.target as HTMLInputElement).value}
              aria-label="New task title"
            />
            <Button type="submit" disabled={!newTaskTitle.value.trim()}>Add</Button>
          </form>
        )}

        {visibleTasks.length > 0 && (
          <ul class="sh-list-card" style={{ listStyle: 'none', margin: 0 }}>
            {visibleTasks.map(t => {
              const due = t.due_date ? dueLabel(t.due_date) : null
              const owners = (t.assignees ?? [])
                .map(uid => userNameById(uid))
                .filter(Boolean)
              const ownerLine =
                owners.length > 0
                  ? owners.join(' · ')
                  : t.created_by ? `+ ${userNameById(t.created_by)}` : ''
              return (
                <li key={t.id}
                    class={`sh-task-row ${t.status === 'done' ? 'sh-task--done' : ''}`}>
                  <input type="checkbox" checked={t.status === 'done'}
                    onChange={() => cycleStatus({
                      ...t,
                      status: t.status === 'done' ? 'todo' : 'done',
                    })}
                    aria-label={`Toggle ${t.title}`} />
                  <button
                    type="button"
                    class="sh-task-title"
                    onClick={() => (editingTask.value = t)}
                    title="Edit task"
                    aria-label={`Edit task: ${t.title}`}
                  >
                    {t.title}
                  </button>
                  <div class="sh-task-meta">
                    {ownerLine && (
                      <span class="sh-byline">{ownerLine}</span>
                    )}
                    {due && due.text !== t.due_date && (
                      <span class="sh-byline sh-byline--accent">
                        · {due.text}
                      </span>
                    )}
                    {due && due.text === t.due_date && (
                      <span class="sh-byline">· {due.text}</span>
                    )}
                    <button type="button"
                      class={`sh-task-status sh-task-status--${t.status}`}
                      onClick={() => void cycleStatus(t)}
                      title="Click to cycle status"
                      aria-label={`Status: ${STATUS_LABEL[t.status as Status]}, click to change`}>
                      {STATUS_LABEL[t.status as Status] ?? t.status}
                    </button>
                  </div>
                  {due?.modifier === 'overdue' && (
                    <span class="sh-task-pin sh-task-pin--overdue">overdue</span>
                  )}
                  {due?.modifier === 'due' && (
                    <span class="sh-task-pin sh-task-pin--due">due</span>
                  )}
                  <button type="button" class="sh-icon-btn"
                          aria-label={`Delete ${t.title}`}
                          onClick={() => void deleteTask(t)}>🗑️</button>
                </li>
              )
            })}
          </ul>
        )}

        {visibleTasks.length === 0 && activeList.value && (
          <div class="sh-empty-state">
            <div aria-hidden="true">✅</div>
            <h3>All caught up</h3>
            <p>No tasks in this list. Type above to add one.</p>
          </div>
        )}
        {!activeList.value && (
          <div class="sh-empty-state">
            <div aria-hidden="true">📋</div>
            <h3>No task lists yet</h3>
            <p>
              Lists keep work pinned to a topic — "House projects",
              "Groceries", "School run". Pick a name and you're off.
            </p>
            <Button
              onClick={() => {
                // Defer a tick so the focus call hits a mounted input
                // (avoids racing with the empty-state render unmount).
                setTimeout(() => newListInputRef.current?.focus(), 0)
              }}
            >
              + Create your first list
            </Button>
          </div>
        )}
      </div>

      {editingTask.value && (
        <TaskEditDialog
          task={editingTask.value}
          onClose={() => (editingTask.value = null)}
          onSaved={(updated) => {
            tasks.value = tasks.value.map(t =>
              t.id === updated.id ? updated : t,
            )
            editingTask.value = null
          }}
        />
      )}
    </div>
  )
}

function ListRow({
  list, active, onSelect, onRename, onDelete,
}: {
  list: TaskListEntry
  active: boolean
  onSelect: () => void
  onRename: (next: string) => void
  onDelete: () => void
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(list.name)
  const inputRef = useRef<HTMLInputElement | null>(null)

  // Auto-focus + select-all when entering edit mode so the existing name
  // is immediately replaceable.
  useEffect(() => {
    if (editing) {
      const el = inputRef.current
      if (el) {
        el.focus()
        el.select()
      }
    }
  }, [editing])

  const commit = () => {
    onRename(draft)
    setEditing(false)
  }
  const cancel = () => {
    setDraft(list.name)
    setEditing(false)
  }

  if (editing) {
    return (
      <div class="sh-task-list-row sh-task-list-row--editing">
        <input
          ref={inputRef}
          type="text"
          value={draft}
          maxLength={120}
          aria-label={`Rename ${list.name}`}
          onInput={(e) => setDraft((e.target as HTMLInputElement).value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') { e.preventDefault(); commit() }
            if (e.key === 'Escape') { e.preventDefault(); cancel() }
          }}
          onBlur={commit}
        />
      </div>
    )
  }

  return (
    <div class="sh-task-list-row">
      <button
        class={`sh-task-list-btn ${active ? 'sh-task-list-btn--active' : ''}`}
        onClick={onSelect}
        onDblClick={() => setEditing(true)}
        title="Click to open · double-click to rename"
      >
        {list.name}
      </button>
      <button
        type="button" class="sh-icon-btn"
        aria-label={`More actions for ${list.name}`}
        onClick={() => setMenuOpen(v => !v)}
        onBlur={() => setTimeout(() => setMenuOpen(false), 120)}
      >⋯</button>
      {menuOpen && (
        <div class="sh-post-menu" role="menu">
          <button role="menuitem"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => { setMenuOpen(false); setDraft(list.name); setEditing(true) }}>
            Rename
          </button>
          <button role="menuitem" class="sh-post-menu-danger"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => { setMenuOpen(false); onDelete() }}>
            Delete list
          </button>
        </div>
      )}
    </div>
  )
}

function TaskEditDialog({
  task, onClose, onSaved,
}: {
  task: TaskItem
  onClose: () => void
  onSaved: (t: TaskItem) => void
}) {
  const title       = signal(task.title)
  const description = signal(task.description ?? '')
  const dueDate     = signal(task.due_date ?? '')
  const status      = signal<Status>(task.status as Status)
  const saving      = signal(false)

  const save = async (e: Event) => {
    e.preventDefault()
    if (!title.value.trim()) {
      showToast('Title cannot be empty', 'error')
      return
    }
    saving.value = true
    try {
      const body: Record<string, unknown> = {
        title:       title.value.trim(),
        description: description.value.trim() || null,
        due_date:    dueDate.value || null,
        status:      status.value,
      }
      const updated = await api.patch(
        `/api/tasks/${task.id}`, body,
      ) as TaskItem
      showToast('Task updated', 'success')
      onSaved(updated)
    } catch (err: unknown) {
      showToast(`Save failed: ${(err as Error).message ?? err}`, 'error')
    } finally {
      saving.value = false
    }
  }

  return (
    <Modal open={true} onClose={onClose} title="Edit task">
      <form onSubmit={save}>
        <label>
          Title
          <input type="text" value={title.value} maxLength={200}
            onInput={(e) => (title.value = (e.target as HTMLInputElement).value)}
            autoFocus />
        </label>
        <label>
          Description
          <textarea value={description.value} maxLength={2000}
            onInput={(e) => (description.value = (e.target as HTMLTextAreaElement).value)} />
        </label>
        <label>
          Due date
          <input type="date" value={dueDate.value}
            onInput={(e) => (dueDate.value = (e.target as HTMLInputElement).value)} />
        </label>
        <label>
          Status
          <div class="sh-task-status-picker" role="radiogroup" aria-label="Status">
            {(['todo', 'in_progress', 'done'] as Status[]).map(s => (
              <button
                key={s}
                type="button"
                role="radio"
                aria-checked={status.value === s}
                class={`sh-task-status sh-task-status--${s} ${status.value === s ? 'sh-task-status--active' : ''}`}
                onClick={() => (status.value = s)}
              >
                {STATUS_LABEL[s]}
              </button>
            ))}
          </div>
        </label>
        <div class="sh-form-actions">
          <Button variant="secondary" onClick={onClose}>Cancel</Button>
          <Button type="submit" loading={saving.value}>Save</Button>
        </div>
      </form>
    </Modal>
  )
}
