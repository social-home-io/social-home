import { useEffect, useMemo, useRef, useState } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import { signal } from '@preact/signals'
import {
  items,
  stores,
  loadShopping,
  wireShoppingWs,
  addItem,
  updateItem,
  toggleItem,
  deleteItem,
  clearCompleted,
  reorderStores,
} from '@/store/shopping'
import type { ShoppingItem } from '@/types'
import { Spinner } from '@/components/Spinner'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { currentUser } from '@/store/auth'
import {
  householdDisplayName,
  loadHouseholdUsers,
} from '@/store/householdUsers'
import { confirmDialog } from '@/components/confirm'
import { relativeDocsTime } from '@/utils/relativeTime'
import { parseItemInput } from '@/utils/shoppingParse'

const loading = signal(true)

/** Key the "Group by store" toggle off ``localStorage`` so the
 *  toggle survives reloads. Default is "auto" — turn on automatically
 *  the first time the household has ≥2 distinct stores; the user can
 *  override either way and the override sticks. */
const GROUP_PREF_KEY = 'sh_shopping_group_by_store'
type GroupPref = 'auto' | 'on' | 'off'

function readGroupPref(): GroupPref {
  try {
    const v = localStorage.getItem(GROUP_PREF_KEY)
    if (v === 'on' || v === 'off') return v
  } catch {
    /* sandboxed */
  }
  return 'auto'
}

function writeGroupPref(v: GroupPref) {
  try {
    if (v === 'auto') localStorage.removeItem(GROUP_PREF_KEY)
    else localStorage.setItem(GROUP_PREF_KEY, v)
  } catch {
    /* sandboxed */
  }
}

const NO_STORE_KEY = '__no_store__'

export default function ShoppingPage() {
  useTitle('Shopping')
  const inputRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    wireShoppingWs()
    void loadHouseholdUsers()
    loadShopping().then(() => { loading.value = false })
  }, [])

  const [draft, setDraft] = useState('')
  const [showSuggest, setShowSuggest] = useState(false)
  const [suggestHeld, setSuggestHeld] = useState(false)
  const [groupPref, setGroupPref] = useState<GroupPref>(readGroupPref())
  const [editingId, setEditingId] = useState<string | null>(null)
  const [dragStore, setDragStore] = useState<string | null>(null)

  // Suggest re-adding any completed item by name (existing pattern).
  const pastNames = useMemo(() => {
    const names = items.value
      .filter(i => i.completed)
      .map(i => (i.text || '').trim())
      .filter(Boolean)
    const seen = new Set<string>()
    const out: string[] = []
    for (const n of names.reverse()) {
      if (seen.has(n)) continue
      seen.add(n)
      out.push(n)
    }
    return out.slice(0, 12)
  }, [items.value])

  const handleQuickAdd = async (e: Event) => {
    e.preventDefault()
    const raw = draft.trim()
    if (!raw) return
    // Comma-split first, then ``@``-split each segment so the user
    // can batch in one go: ``Milk @ Aldi, Bread @ Bakery, Eggs``.
    const parts = raw
      .split(',')
      .map(s => parseItemInput(s))
      .filter(p => p.text)
    if (parts.length === 0) return
    const existing = new Set(
      items.value
        .filter(i => !i.completed)
        .map(i => (i.text || '').toLowerCase()),
    )
    const dupes: string[] = []
    try {
      for (const { text, store } of parts) {
        if (existing.has(text.toLowerCase())) {
          dupes.push(text)
          continue
        }
        await addItem(text, store)
        existing.add(text.toLowerCase())
      }
      setDraft('')
      setShowSuggest(false)
      if (dupes.length && parts.length === dupes.length) {
        showToast('All items are already on the list', 'info')
      } else if (dupes.length) {
        showToast(`${dupes.length} duplicate skipped`, 'info')
      }
    } catch (err: unknown) {
      showToast(`Add failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  const addSuggestion = async (name: string) => {
    try {
      await addItem(name)
      inputRef.current?.focus()
    } catch (err: unknown) {
      showToast(`Add failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  const handleToggle = async (id: string, completed: boolean) => {
    try {
      await toggleItem(id, !completed)
    } catch (err: unknown) {
      showToast(`Update failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  const handleDelete = async (id: string) => {
    try {
      await deleteItem(id)
    } catch (err: unknown) {
      showToast(`Delete failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  const handleClearCompleted = async () => {
    if (!await confirmDialog('Clear all completed items? This cannot be undone.', { destructive: true })) return
    try {
      await clearCompleted()
    } catch (err: unknown) {
      showToast(`Clear failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  const handleEditSave = async (
    id: string,
    nextText: string,
    nextStore: string,
  ) => {
    const trimmedText = nextText.trim()
    if (!trimmedText) return
    const trimmedStore = nextStore.trim()
    try {
      await updateItem(id, {
        text: trimmedText,
        store: trimmedStore || null,
      })
      setEditingId(null)
    } catch (err: unknown) {
      showToast(`Update failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  const handleMoveStore = async (name: string, delta: number) => {
    const order = stores.value.map(s => s.name)
    const i = order.indexOf(name)
    if (i < 0) return
    const j = i + delta
    if (j < 0 || j >= order.length) return
    const next = order.slice()
    ;[next[i], next[j]] = [next[j], next[i]]
    try {
      await reorderStores(next)
    } catch (err: unknown) {
      showToast(`Reorder failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  const handleDropOnStore = async (target: string) => {
    if (!dragStore || dragStore === target) {
      setDragStore(null)
      return
    }
    const order = stores.value.map(s => s.name)
    const from = order.indexOf(dragStore)
    const to = order.indexOf(target)
    if (from < 0 || to < 0) {
      setDragStore(null)
      return
    }
    const next = order.slice()
    next.splice(from, 1)
    next.splice(to, 0, dragStore)
    setDragStore(null)
    try {
      await reorderStores(next)
    } catch (err: unknown) {
      showToast(`Reorder failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  // Autofocus on mount so keyboard flow ("open page → start typing →
  // Enter → repeat") works without an extra click.
  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  if (loading.value) return <Spinner />

  const active    = items.value.filter(i => !i.completed)
  const completed = items.value.filter(i =>  i.completed)
  const me        = currentUser.value

  const userNameById = (uid: string): string =>
    me?.user_id === uid ? 'you' : householdDisplayName(uid)

  // Group rendering kicks in when the catalogue has ≥2 stores OR the
  // user explicitly toggled it on. ``auto`` (default) flips ON as
  // soon as a second store appears; the explicit ``on`` / ``off``
  // overrides stick.
  const distinctStores = useMemo(() => {
    const s = new Set<string>()
    for (const i of items.value) if (i.store) s.add(i.store)
    return s
  }, [items.value])
  const grouped =
    groupPref === 'on' ||
    (groupPref === 'auto' && (stores.value.length >= 2 || distinctStores.size >= 2))

  const setGroupedPref = (next: 'on' | 'off') => {
    setGroupPref(next)
    writeGroupPref(next)
  }

  // Datalist input id for the inline-edit store field. Putting it
  // once at the page root keeps the DOM clean even when several
  // rows mount editors over a session.
  const storeListId = 'sh-shopping-store-suggest'

  return (
    <div class="sh-shopping">
      <div class="sh-shopping-header">
        <span class="sh-muted">
          {active.length} to buy · {completed.length} done
        </span>
        {(stores.value.length > 0 || distinctStores.size > 0) && (
          <div class="sh-shopping-grouptoggle" role="group" aria-label="View mode">
            <button
              type="button"
              class={'sh-chip ' + (grouped ? 'sh-chip--active' : '')}
              onClick={() => setGroupedPref('on')}
            >
              Group by store
            </button>
            <button
              type="button"
              class={'sh-chip ' + (!grouped ? 'sh-chip--active' : '')}
              onClick={() => setGroupedPref('off')}
            >
              Show as list
            </button>
          </div>
        )}
      </div>

      <form onSubmit={handleQuickAdd} class="sh-shopping-add">
        <input
          ref={inputRef}
          name="text"
          value={draft}
          placeholder="Add one — or paste several. Tip: end with @ Store"
          autoComplete="off"
          onInput={(e) => setDraft((e.target as HTMLInputElement).value)}
          onFocus={() => setShowSuggest(true)}
          onBlur={() => {
            setTimeout(() => {
              if (!suggestHeld) setShowSuggest(false)
            }, 120)
          }}
          aria-label="New shopping item"
        />
        <Button type="submit" disabled={!draft.trim()}>Add</Button>
      </form>

      {showSuggest && pastNames.length > 0 && (
        <div
          class="sh-shopping-suggest" role="listbox"
          onMouseDown={() => setSuggestHeld(true)}
          onMouseUp={() => setSuggestHeld(false)}
        >
          <span class="sh-muted">Re-add recent:</span>
          {pastNames.map((name) => (
            <button
              key={name}
              type="button"
              class="sh-chip"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => void addSuggestion(name)}
            >
              {name}
            </button>
          ))}
        </div>
      )}

      {/* Single shared datalist for the inline-edit store input —
          autosuggests every known catalogue name. */}
      <datalist id={storeListId}>
        {stores.value.map((s) => (
          <option key={s.name} value={s.name} />
        ))}
      </datalist>

      {items.value.length === 0 ? (
        <div class="sh-empty-state">
          <div aria-hidden="true">🛒</div>
          <h3>Your list is empty</h3>
          <p>Type an item above. Paste multiple, separated by commas.</p>
        </div>
      ) : grouped ? (
        <GroupedView
          active={active}
          completed={completed}
          editingId={editingId}
          onEditStart={setEditingId}
          onEditCancel={() => setEditingId(null)}
          onEditSave={handleEditSave}
          onToggle={handleToggle}
          onDelete={handleDelete}
          onClearCompleted={handleClearCompleted}
          onDragStoreStart={setDragStore}
          onDropOnStore={handleDropOnStore}
          dragStore={dragStore}
          onMoveStore={handleMoveStore}
          userNameById={userNameById}
          storeListId={storeListId}
        />
      ) : (
        <FlatView
          active={active}
          completed={completed}
          editingId={editingId}
          onEditStart={setEditingId}
          onEditCancel={() => setEditingId(null)}
          onEditSave={handleEditSave}
          onToggle={handleToggle}
          onDelete={handleDelete}
          onClearCompleted={handleClearCompleted}
          userNameById={userNameById}
          storeListId={storeListId}
        />
      )}
    </div>
  )
}

// ─── Flat / ungrouped rendering (existing single-list layout) ──────────

interface ViewProps {
  active: ShoppingItem[]
  completed: ShoppingItem[]
  editingId: string | null
  onEditStart: (id: string) => void
  onEditCancel: () => void
  onEditSave: (id: string, text: string, store: string) => void
  onToggle: (id: string, completed: boolean) => void
  onDelete: (id: string) => void
  onClearCompleted: () => void
  userNameById: (uid: string) => string
  storeListId: string
}

function FlatView(props: ViewProps) {
  const renderRow = (item: ShoppingItem, done: boolean) => (
    <ItemRow
      key={item.id}
      item={item}
      done={done}
      isEditing={props.editingId === item.id}
      onEditStart={() => props.onEditStart(item.id)}
      onEditCancel={props.onEditCancel}
      onEditSave={(t, s) => props.onEditSave(item.id, t, s)}
      onToggle={() => props.onToggle(item.id, item.completed)}
      onDelete={() => props.onDelete(item.id)}
      userNameById={props.userNameById}
      storeListId={props.storeListId}
      showStorePill
    />
  )
  return (
    <>
      <ul class="sh-shopping-list sh-list-card">
        {props.active.map(i => renderRow(i, false))}
      </ul>
      {props.completed.length > 0 && (
        <>
          <div class="sh-shopping-divider">
            <span>Already bought ({props.completed.length})</span>
            <button
              type="button"
              class="sh-link"
              onClick={props.onClearCompleted}
            >
              Clear all
            </button>
          </div>
          <ul class="sh-shopping-list sh-list-card sh-list-card--moss sh-shopping-list--done">
            {props.completed.map(i => renderRow(i, true))}
          </ul>
        </>
      )}
    </>
  )
}

// ─── Grouped-by-store rendering ────────────────────────────────────────

interface GroupedProps extends ViewProps {
  onDragStoreStart: (name: string | null) => void
  onDropOnStore: (target: string) => void
  dragStore: string | null
  onMoveStore: (name: string, delta: number) => void
}

function GroupedView(props: GroupedProps) {
  // Build an ordered list of section keys. Catalogue stores first
  // (in their sort_order), then the synthetic "No store" bucket.
  const sections: { key: string; label: string; draggable: boolean }[] = [
    ...stores.value.map((s) => ({
      key: s.name,
      label: s.name,
      draggable: true,
    })),
    { key: NO_STORE_KEY, label: 'No store', draggable: false },
  ]

  return (
    <>
      {sections.map((section, idx) => {
        const itemsHere = props.active.filter((i) =>
          section.key === NO_STORE_KEY
            ? !i.store
            : i.store === section.key,
        )
        const doneHere = props.completed.filter((i) =>
          section.key === NO_STORE_KEY
            ? !i.store
            : i.store === section.key,
        )
        if (itemsHere.length === 0 && doneHere.length === 0) return null
        const isFirst = idx === 0
        const isLast = idx === stores.value.length - 1 // before "No store"
        return (
          <section
            key={section.key}
            class={
              'sh-shopping-group ' +
              (props.dragStore === section.key
                ? 'sh-shopping-group--dragging '
                : '') +
              (section.key === NO_STORE_KEY ? 'sh-shopping-group--unassigned' : '')
            }
            onDragOver={(e) => {
              if (props.dragStore && section.draggable) {
                e.preventDefault()
              }
            }}
            onDrop={(e) => {
              if (!section.draggable) return
              e.preventDefault()
              props.onDropOnStore(section.key)
            }}
          >
            <header
              class="sh-shopping-group__header"
              draggable={section.draggable}
              onDragStart={() => {
                if (section.draggable) props.onDragStoreStart(section.key)
              }}
              onDragEnd={() => props.onDragStoreStart(null)}
            >
              {section.draggable && (
                <span
                  class="sh-shopping-group__drag"
                  aria-hidden="true"
                  title="Drag to reorder"
                >
                  ⋮⋮
                </span>
              )}
              <h3 class="sh-shopping-group__name">{section.label}</h3>
              <span class="sh-shopping-group__count">
                {itemsHere.length}
                {doneHere.length > 0 ? ` · ${doneHere.length} done` : ''}
              </span>
              {section.draggable && (
                <div class="sh-shopping-group__nudge" role="group" aria-label="Reorder">
                  <button
                    type="button"
                    class="sh-shopping-group__nudge-btn"
                    aria-label="Move up"
                    disabled={isFirst}
                    onClick={() => props.onMoveStore(section.key, -1)}
                  >
                    ▲
                  </button>
                  <button
                    type="button"
                    class="sh-shopping-group__nudge-btn"
                    aria-label="Move down"
                    disabled={isLast}
                    onClick={() => props.onMoveStore(section.key, +1)}
                  >
                    ▼
                  </button>
                </div>
              )}
            </header>

            {itemsHere.length > 0 && (
              <ul class="sh-shopping-list sh-list-card">
                {itemsHere.map((item) => (
                  <ItemRow
                    key={item.id}
                    item={item}
                    done={false}
                    isEditing={props.editingId === item.id}
                    onEditStart={() => props.onEditStart(item.id)}
                    onEditCancel={props.onEditCancel}
                    onEditSave={(t, s) => props.onEditSave(item.id, t, s)}
                    onToggle={() => props.onToggle(item.id, item.completed)}
                    onDelete={() => props.onDelete(item.id)}
                    userNameById={props.userNameById}
                    storeListId={props.storeListId}
                    showStorePill={false}
                  />
                ))}
              </ul>
            )}
            {doneHere.length > 0 && (
              <ul class="sh-shopping-list sh-list-card sh-list-card--moss sh-shopping-list--done">
                {doneHere.map((item) => (
                  <ItemRow
                    key={item.id}
                    item={item}
                    done={true}
                    isEditing={props.editingId === item.id}
                    onEditStart={() => props.onEditStart(item.id)}
                    onEditCancel={props.onEditCancel}
                    onEditSave={(t, s) => props.onEditSave(item.id, t, s)}
                    onToggle={() => props.onToggle(item.id, item.completed)}
                    onDelete={() => props.onDelete(item.id)}
                    userNameById={props.userNameById}
                    storeListId={props.storeListId}
                    showStorePill={false}
                  />
                ))}
              </ul>
            )}
          </section>
        )
      })}
      {props.completed.length > 0 && (
        <div class="sh-shopping-divider">
          <span>{props.completed.length} bought</span>
          <button
            type="button"
            class="sh-link"
            onClick={props.onClearCompleted}
          >
            Clear all
          </button>
        </div>
      )}
    </>
  )
}

// ─── Item row + inline edit ────────────────────────────────────────────

interface RowProps {
  item: ShoppingItem
  done: boolean
  isEditing: boolean
  onEditStart: () => void
  onEditCancel: () => void
  onEditSave: (text: string, store: string) => void
  onToggle: () => void
  onDelete: () => void
  userNameById: (uid: string) => string
  storeListId: string
  showStorePill: boolean
}

function ItemRow(props: RowProps) {
  const { item, done } = props
  if (props.isEditing) return (
    <li class="sh-shopping-item sh-shopping-item--edit">
      <EditRow
        initialText={item.text}
        initialStore={item.store ?? ''}
        storeListId={props.storeListId}
        onSave={props.onEditSave}
        onCancel={props.onEditCancel}
      />
    </li>
  )

  return (
    <li class={'sh-shopping-item ' + (done ? 'sh-item--done' : '')}>
      <label class="sh-shopping-item__main">
        <input
          type="checkbox"
          checked={done}
          onChange={props.onToggle}
          aria-label={
            done
              ? `Put ${item.text} back on the list`
              : `Mark ${item.text} as bought`
          }
        />
        <span
          class="sh-shopping-item__text"
          onClick={(e) => {
            e.preventDefault()
            props.onEditStart()
          }}
          title="Click to edit"
        >
          {item.text}
        </span>
      </label>
      {props.showStorePill && item.store && (
        <span class="sh-shopping-store-pill" title={`Buy at ${item.store}`}>
          <span aria-hidden="true">📍</span> {item.store}
        </span>
      )}
      <div
        class="sh-shopping-item__meta"
        title={
          item.created_at
            ? `Added ${relativeDocsTime(item.created_at)}`
            : undefined
        }
      >
        {item.created_by && (
          <span>+ {props.userNameById(item.created_by)}</span>
        )}
      </div>
      <button
        type="button"
        class="sh-shopping-item__delete"
        aria-label={`Delete ${item.text}`}
        title="Delete"
        onClick={props.onDelete}
      >
        ✕
      </button>
    </li>
  )
}

function EditRow({
  initialText,
  initialStore,
  storeListId,
  onSave,
  onCancel,
}: {
  initialText: string
  initialStore: string
  storeListId: string
  onSave: (text: string, store: string) => void
  onCancel: () => void
}) {
  const [text, setText] = useState(initialText)
  const [store, setStore] = useState(initialStore)
  const textRef = useRef<HTMLInputElement | null>(null)
  useEffect(() => {
    textRef.current?.focus()
    textRef.current?.select()
  }, [])
  const submit = () => onSave(text, store)
  const cancel = () => onCancel()
  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      submit()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      cancel()
    }
  }
  return (
    <div class="sh-shopping-edit">
      <input
        ref={textRef}
        type="text"
        value={text}
        aria-label="Item text"
        onInput={(e) => setText((e.target as HTMLInputElement).value)}
        onKeyDown={onKey}
        class="sh-shopping-edit__text"
      />
      <input
        type="text"
        value={store}
        list={storeListId}
        placeholder="Store (optional)"
        aria-label="Store"
        onInput={(e) => setStore((e.target as HTMLInputElement).value)}
        onKeyDown={onKey}
        class="sh-shopping-edit__store"
      />
      <Button type="button" onClick={submit} disabled={!text.trim()}>Save</Button>
      <button
        type="button"
        class="sh-link sh-shopping-edit__cancel"
        onClick={cancel}
      >
        Cancel
      </button>
    </div>
  )
}
