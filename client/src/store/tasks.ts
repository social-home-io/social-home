/**
 * Tasks store — keeps the current task-list + task grid in sync with
 * WS frames from the backend.
 *
 * Emitted events the store subscribes to:
 *
 *  * ``task.created``      — a new task appears in some list
 *  * ``task.updated``      — any field changed (title, status, due,
 *                            assignees, position, description)
 *  * ``task.deleted``      — task row removed
 *  * ``task.assigned``     — a user newly assigned (fires alongside
 *                            ``task.updated`` for UI side-effects)
 *  * ``task.completed``    — transitioned to DONE
 *  * ``task.deadline_due`` — the deadline scheduler fired
 *  * ``task_list.created`` / ``.updated`` / ``.deleted`` — list
 *                            sidebar changes
 *
 * The page component writes the initial REST response into ``tasks``
 * and ``lists`` and then lets this store merge live updates.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import type { TaskItem, TaskListEntry } from '@/types'

export const lists = signal<TaskListEntry[]>([])
export const tasks = signal<TaskItem[]>([])

/** Optimistic status patch — flips the row locally first so the
 *  user's drag visually lands the task in the new section the
 *  instant they release the mouse, then sends the PATCH. On error
 *  the row rolls back and a toast surfaces the failure. Mirrors
 *  the shopping list's ``updateItem`` shape.
 *
 *  Re-throws so callers can stop propagation (e.g. cancel a
 *  follow-up animation) but the rollback has already happened by
 *  the time the throw lands. */
export async function patchTaskStatus(
  taskId: string,
  nextStatus: 'todo' | 'in_progress' | 'done',
): Promise<void> {
  const prev = tasks.value
  const target = prev.find(t => t.id === taskId)
  if (!target || target.status === nextStatus) return
  // Optimistic — apply locally first.
  tasks.value = prev.map(t => t.id === taskId ? { ...t, status: nextStatus } : t)
  try {
    const updated = await api.patch(
      `/api/tasks/${taskId}`, { status: nextStatus },
    ) as TaskItem
    // Reconcile with server's canonical shape (might include updated
    // ``updated_at`` etc. that the optimistic copy didn't carry).
    tasks.value = tasks.value.map(t => t.id === taskId ? updated : t)
  } catch (err) {
    tasks.value = prev
    throw err
  }
}

/** Delete all done tasks in a given list in parallel. The backend
 *  has no atomic clear-completed endpoint for tasks (yet), so we
 *  loop the existing ``DELETE /api/tasks/{id}``. Optimistic: drop
 *  the rows locally first so the section empties immediately; on
 *  any failure restore the lost rows and surface the count.
 *
 *  Returns ``{ ok, failed }`` so the caller can pick a toast.
 *  Typical household task lists have ≤10 done items so the
 *  N-request fan-out is acceptable; if it ever becomes a hotspot
 *  the backend can ship a single ``POST /api/tasks/lists/{id}/clear-completed``
 *  and this helper becomes a one-call replacement. */
export async function clearCompletedTasks(
  listId: string,
): Promise<{ ok: number; failed: number }> {
  const prev = tasks.value
  const toRemove = prev.filter(t => t.list_id === listId && t.status === 'done')
  if (toRemove.length === 0) return { ok: 0, failed: 0 }
  const removedIds = new Set(toRemove.map(t => t.id))
  tasks.value = prev.filter(t => !removedIds.has(t.id))
  const results = await Promise.allSettled(
    toRemove.map(t => api.delete(`/api/tasks/${t.id}`)),
  )
  const failedIds: string[] = []
  results.forEach((r, i) => {
    if (r.status === 'rejected') failedIds.push(toRemove[i].id)
  })
  if (failedIds.length > 0) {
    // Restore just the failed rows so the user sees what's still
    // there. Keeps the successful deletes in place — partial
    // success shouldn't wipe a half-finished clear.
    const failedSet = new Set(failedIds)
    const restored = prev.filter(t => failedSet.has(t.id))
    tasks.value = [...tasks.value, ...restored]
  }
  return {
    ok:     results.length - failedIds.length,
    failed: failedIds.length,
  }
}

export function wireTasksWs(): void {
  ws.on('task.created', (e) => {
    const t = (e.data as { task: TaskItem }).task
    if (!t) return
    if (!tasks.value.some((x) => x.id === t.id)) {
      tasks.value = [...tasks.value, t]
    }
  })
  ws.on('task.updated', (e) => {
    const t = (e.data as { task: TaskItem }).task
    if (!t) return
    if (tasks.value.some((x) => x.id === t.id)) {
      tasks.value = tasks.value.map((x) => (x.id === t.id ? t : x))
    } else {
      tasks.value = [...tasks.value, t]
    }
  })
  ws.on('task.deleted', (e) => {
    const { task_id } = e.data as { task_id: string }
    if (!task_id) return
    tasks.value = tasks.value.filter((x) => x.id !== task_id)
  })
  ws.on('task.completed', (e) => {
    const { task_id } = e.data as { task_id: string }
    tasks.value = tasks.value.map((x) =>
      x.id === task_id ? { ...x, status: 'done' } : x,
    )
  })
  ws.on('task.deadline_due', (_e) => {
    // Notification service already writes a row; store hook kept for
    // consumers that want to highlight the task inline.
  })

  ws.on('task_list.created', (e) => {
    const { list_id, name } = e.data as { list_id: string; name: string }
    if (!list_id) return
    if (!lists.value.some((x) => x.id === list_id)) {
      lists.value = [...lists.value, { id: list_id, name }]
    }
  })
  ws.on('task_list.updated', (e) => {
    const { list_id, name } = e.data as { list_id: string; name: string }
    if (!list_id) return
    lists.value = lists.value.map((x) =>
      x.id === list_id ? { ...x, name } : x,
    )
  })
  ws.on('task_list.deleted', (e) => {
    const { list_id } = e.data as { list_id: string }
    if (!list_id) return
    lists.value = lists.value.filter((x) => x.id !== list_id)
    tasks.value = tasks.value.filter((x) => x.list_id !== list_id)
  })
}
