import { describe, it, expect, vi, beforeEach } from 'vitest'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiPatch = vi.fn()
const apiDelete = vi.fn()

vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
    patch: (...args: unknown[]) => apiPatch(...args),
    delete: (...args: unknown[]) => apiDelete(...args),
  },
}))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

vi.mock('@/store/householdUsers', () => ({
  householdUsers: { value: new Map() },
  loadHouseholdUsers: vi.fn().mockResolvedValue(undefined),
  householdDisplayName: (uid: string) => uid,
}))

vi.mock('@/components/confirm', () => ({
  confirmDialog: vi.fn().mockResolvedValue(true),
}))

interface Fixtures {
  lists: unknown[]
  tasks: unknown[]
}

function wireApi(f: Fixtures): void {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/tasks/lists') return f.lists
    if (url.startsWith('/api/tasks/lists/') && url.endsWith('/tasks')) {
      return f.tasks
    }
    return []
  })
  apiPost.mockResolvedValue({})
  apiPatch.mockImplementation(async (_url: string, body: unknown) => {
    // The page expects PATCH responses to echo the updated task —
    // return a merged shape so reassignments persist in the store.
    return { ...(f.tasks[0] as object), ...(body as object) }
  })
  apiDelete.mockResolvedValue(undefined)
}

function makeDataTransfer(): DataTransfer {
  const store = new Map<string, string>()
  const types: string[] = []
  return {
    types,
    effectAllowed: 'all',
    setData(type: string, val: string) {
      store.set(type, val)
      if (!types.includes(type)) types.push(type)
    },
    getData(type: string) { return store.get(type) ?? '' },
  } as unknown as DataTransfer
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  apiPost.mockReset()
  apiPatch.mockReset()
  apiDelete.mockReset()
})

describe('TaskPage', () => {
  it('module exports a default component', async () => {
    const mod = await import('./TaskPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  })

  it('renders three status group headers when a list has tasks', async () => {
    wireApi({
      lists: [{ id: 'l1', name: 'House' }],
      tasks: [
        { id: 't1', list_id: 'l1', title: 'Fix tap',  status: 'todo',        position: 1, assignees: [], created_by: 'u1' },
        { id: 't2', list_id: 'l1', title: 'Paint',    status: 'in_progress', position: 2, assignees: [], created_by: 'u1' },
        { id: 't3', list_id: 'l1', title: 'Pay bill', status: 'done',        position: 3, assignees: [], created_by: 'u1' },
      ],
    })
    const { render, waitFor } = await import('@testing-library/preact')
    const mod = await import('./TaskPage')
    const { container } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelectorAll('.sh-task-group').length).toBe(3)
    }, { timeout: 2000 })
    const headers = Array.from(container.querySelectorAll('.sh-task-group__name'))
      .map(h => h.textContent)
    expect(headers).toEqual(['To do', 'In progress', 'Done'])
  })

  it('places each task under its current status group', async () => {
    wireApi({
      lists: [{ id: 'l1', name: 'House' }],
      tasks: [
        { id: 't1', list_id: 'l1', title: 'Fix tap',  status: 'todo',        position: 1, assignees: [], created_by: 'u1' },
        { id: 't2', list_id: 'l1', title: 'Paint',    status: 'in_progress', position: 2, assignees: [], created_by: 'u1' },
      ],
    })
    const { render, waitFor } = await import('@testing-library/preact')
    const mod = await import('./TaskPage')
    const { container } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelectorAll('.sh-task-row').length).toBe(2)
    })
    const todo = container.querySelector('.sh-task-group--todo')
    const ip   = container.querySelector('.sh-task-group--in_progress')
    expect(todo?.textContent).toContain('Fix tap')
    expect(ip?.textContent).toContain('Paint')
  })

  it('drag-drop on a status section calls PATCH with the new status', async () => {
    wireApi({
      lists: [{ id: 'l1', name: 'House' }],
      tasks: [
        { id: 't1', list_id: 'l1', title: 'Fix tap', status: 'todo', position: 1, assignees: [], created_by: 'u1' },
      ],
    })
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./TaskPage')
    const { container } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelector('.sh-task-row')).not.toBeNull()
    })
    const row = container.querySelector('.sh-task-row') as HTMLElement
    expect(row.getAttribute('draggable')).toBe('true')
    const dataTransfer = makeDataTransfer()
    fireEvent.dragStart(row, { dataTransfer })
    const ipGroup = container.querySelector('.sh-task-group--in_progress') as HTMLElement
    expect(ipGroup).toBeTruthy()
    fireEvent.dragOver(ipGroup, { dataTransfer })
    fireEvent.drop(ipGroup, { dataTransfer })
    await waitFor(() => {
      expect(apiPatch).toHaveBeenCalledWith(
        '/api/tasks/t1',
        { status: 'in_progress' },
      )
    })
  })

  it('edit dialog status picker actually changes the saved status (signal-in-render bug)', async () => {
    // Regression for the bug where ``signal()`` was created inside
    // the component body — every re-render replaced the signal
    // with a fresh one initialised to ``task.status``, so clicking
    // a different status button visually toggled for one frame
    // then bounced right back to the original.
    wireApi({
      lists: [{ id: 'l1', name: 'House' }],
      tasks: [
        { id: 't1', list_id: 'l1', title: 'Fix tap', status: 'todo', position: 1, assignees: [], created_by: 'u1' },
      ],
    })
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./TaskPage')
    const { container } = render(<mod.default />)
    await waitFor(() => {
      expect(container.querySelector('.sh-task-title')).not.toBeNull()
    })

    // Open the edit dialog by clicking the task title.
    fireEvent.click(container.querySelector('.sh-task-title') as HTMLElement)
    await waitFor(() => {
      expect(container.querySelector('.sh-task-status-picker')).not.toBeNull()
    })

    // Pick "Done" in the picker.
    const doneBtn = Array.from(
      container.querySelectorAll('.sh-task-status-picker button'),
    ).find(b => b.textContent === 'Done') as HTMLElement
    expect(doneBtn).toBeTruthy()
    fireEvent.click(doneBtn)

    // After the click, the Done button should be the active one —
    // the bug would leave "To do" active because the signal reset.
    await waitFor(() => {
      expect(doneBtn.classList.contains('sh-task-status--active')).toBe(true)
    })

    // Submit the form and confirm PATCH payload carries the new
    // status. Without the fix, the body would still carry
    // ``status: 'todo'`` because the dialog never saw the click.
    // Click the Save button rather than submitting via JS — the
    // form's ``onSubmit`` is wired to ``Button type="submit"``,
    // and submit events synthesised against the form root don't
    // always trigger that path in jsdom.
    const saveBtn = Array.from(container.querySelectorAll('button'))
      .find(b => b.textContent === 'Save' && b.getAttribute('type') === 'submit') as HTMLButtonElement
    expect(saveBtn).toBeTruthy()
    fireEvent.click(saveBtn)
    await waitFor(() => {
      const calls = apiPatch.mock.calls.filter(
        ([url]) => url === '/api/tasks/t1',
      )
      const lastBody = calls[calls.length - 1]?.[1] as { status?: string } | undefined
      expect(lastBody?.status).toBe('done')
    })
  })
})
