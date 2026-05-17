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

/** Distinguishes item drags from store-header drags in the same
 *  drag-and-drop layer. Stamped on ``dataTransfer`` so a section's
 *  drop handler can decide whether to reassign an item or reorder
 *  stores. The string is opaque — we only look at the *presence* of
 *  ``DRAG_ITEM_MIME``. */
const DRAG_ITEM_MIME = 'application/x-sh-shopping-item'
const DRAG_STORE_MIME = 'application/x-sh-shopping-store'

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
  /** Currently-dragged store name (header drag), or ``null``. */
  const [dragStore, setDragStore] = useState<string | null>(null)
  /** Currently-dragged item id (row drag), or ``null``. ``null`` and
   *  ``dragStore=null`` together mean nothing is being dragged. The
   *  two states are mutually exclusive: a single drag is *either* a
   *  store reorder or an item reassign, never both. */
  const [dragItemId, setDragItemId] = useState<string | null>(null)
  /** Section currently hovered as a drop target while a row is being
   *  dragged. Drives the section's drop-zone highlight. */
  const [dropTarget, setDropTarget] = useState<string | null>(null)

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

  /** Rename-only save path. The store assignment moved to the
   *  ``StorePicker`` popover (single-tap on the row's store pill) so
   *  this entry point doesn't need a second field. */
  const handleEditSave = async (id: string, nextText: string) => {
    const trimmedText = nextText.trim()
    if (!trimmedText) return
    try {
      await updateItem(id, { text: trimmedText })
      setEditingId(null)
    } catch (err: unknown) {
      showToast(`Update failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  /** Reassign an item to a different store (or clear the store).
   *  Drives both the StorePicker popover and the drag-and-drop path
   *  in GroupedView — both call straight here. ``null`` clears the
   *  store ("No store"). */
  const handleReassignStore = async (
    id: string,
    nextStore: string | null,
  ) => {
    try {
      await updateItem(id, { store: nextStore })
    } catch (err: unknown) {
      showToast(`Reassign failed: ${(err as Error)?.message ?? err}`, 'error')
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

  const handleStoreHeaderDrop = async (target: string) => {
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

  const storeNames = stores.value.map(s => s.name)

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
          onReassignStore={handleReassignStore}
          onDragStoreStart={setDragStore}
          onStoreHeaderDrop={handleStoreHeaderDrop}
          dragStore={dragStore}
          onMoveStore={handleMoveStore}
          dragItemId={dragItemId}
          onDragItemStart={setDragItemId}
          onDragItemEnd={() => { setDragItemId(null); setDropTarget(null) }}
          dropTarget={dropTarget}
          onDropTargetChange={setDropTarget}
          userNameById={userNameById}
          storeNames={storeNames}
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
          onReassignStore={handleReassignStore}
          userNameById={userNameById}
          storeNames={storeNames}
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
  onEditSave: (id: string, text: string) => void
  onToggle: (id: string, completed: boolean) => void
  onDelete: (id: string) => void
  onClearCompleted: () => void
  onReassignStore: (id: string, nextStore: string | null) => void
  userNameById: (uid: string) => string
  storeNames: string[]
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
      onEditSave={(t) => props.onEditSave(item.id, t)}
      onToggle={() => props.onToggle(item.id, item.completed)}
      onDelete={() => props.onDelete(item.id)}
      onReassignStore={(s) => props.onReassignStore(item.id, s)}
      userNameById={props.userNameById}
      storeNames={props.storeNames}
      draggable={false}
      onDragItemStart={null}
      onDragItemEnd={null}
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
  onStoreHeaderDrop: (target: string) => void
  dragStore: string | null
  onMoveStore: (name: string, delta: number) => void
  dragItemId: string | null
  onDragItemStart: (id: string) => void
  onDragItemEnd: () => void
  dropTarget: string | null
  onDropTargetChange: (target: string | null) => void
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
        // Hide a section only when it's completely empty AND no item
        // drag is in flight — during an item drag every section needs
        // to be a visible drop target, even ones that have no items
        // yet, so the user can drag "Eggs" to a freshly-created store.
        if (
          itemsHere.length === 0
          && doneHere.length === 0
          && props.dragItemId === null
        ) return null
        const isFirst = idx === 0
        const isLast = idx === stores.value.length - 1 // before "No store"
        const isDropTarget = props.dropTarget === section.key
        return (
          <section
            key={section.key}
            class={
              'sh-shopping-group ' +
              (props.dragStore === section.key
                ? 'sh-shopping-group--dragging '
                : '') +
              (isDropTarget ? 'sh-shopping-group--drop-target ' : '') +
              (section.key === NO_STORE_KEY ? 'sh-shopping-group--unassigned' : '')
            }
            onDragOver={(e) => {
              // Two drag shapes converge on the same dragover: store
              // headers (existing reorder) and item rows (the new
              // reassign path). Both must call preventDefault to make
              // this element a valid drop target.
              const types = e.dataTransfer?.types
              const isItemDrag = !!types?.includes(DRAG_ITEM_MIME)
              const isStoreDrag = !!types?.includes(DRAG_STORE_MIME)
              if (isItemDrag) {
                e.preventDefault()
                if (props.dropTarget !== section.key) {
                  props.onDropTargetChange(section.key)
                }
              } else if (isStoreDrag && section.draggable) {
                e.preventDefault()
              }
            }}
            onDragLeave={(e) => {
              // ``dragleave`` fires when crossing into a child element
              // too. Only clear the drop highlight when the cursor
              // really left the section root.
              if (e.currentTarget === e.target) {
                if (props.dropTarget === section.key) {
                  props.onDropTargetChange(null)
                }
              }
            }}
            onDrop={(e) => {
              const types = e.dataTransfer?.types
              if (types?.includes(DRAG_ITEM_MIME)) {
                e.preventDefault()
                const id = e.dataTransfer?.getData(DRAG_ITEM_MIME)
                if (id) {
                  props.onReassignStore(
                    id,
                    section.key === NO_STORE_KEY ? null : section.key,
                  )
                }
                props.onDropTargetChange(null)
              } else if (types?.includes(DRAG_STORE_MIME) && section.draggable) {
                e.preventDefault()
                props.onStoreHeaderDrop(section.key)
              }
            }}
          >
            <header
              class="sh-shopping-group__header"
              draggable={section.draggable}
              onDragStart={(e) => {
                if (!section.draggable) return
                e.dataTransfer?.setData(DRAG_STORE_MIME, section.key)
                props.onDragStoreStart(section.key)
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

            {/* Empty-during-drag placeholder so a freshly-created
             *  section without items still reads as a valid drop
             *  target while the user is mid-drag. */}
            {itemsHere.length === 0
              && doneHere.length === 0
              && props.dragItemId !== null && (
              <div class="sh-shopping-group__droppad" aria-hidden="true">
                Drop here to move into {section.label}
              </div>
            )}

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
                    onEditSave={(t) => props.onEditSave(item.id, t)}
                    onToggle={() => props.onToggle(item.id, item.completed)}
                    onDelete={() => props.onDelete(item.id)}
                    onReassignStore={(s) => props.onReassignStore(item.id, s)}
                    userNameById={props.userNameById}
                    storeNames={props.storeNames}
                    draggable={true}
                    onDragItemStart={props.onDragItemStart}
                    onDragItemEnd={props.onDragItemEnd}
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
                    onEditSave={(t) => props.onEditSave(item.id, t)}
                    onToggle={() => props.onToggle(item.id, item.completed)}
                    onDelete={() => props.onDelete(item.id)}
                    onReassignStore={(s) => props.onReassignStore(item.id, s)}
                    userNameById={props.userNameById}
                    storeNames={props.storeNames}
                    draggable={false}
                    onDragItemStart={null}
                    onDragItemEnd={null}
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
  onEditSave: (text: string) => void
  onToggle: () => void
  onDelete: () => void
  onReassignStore: (nextStore: string | null) => void
  userNameById: (uid: string) => string
  storeNames: string[]
  /** When true, the whole row carries HTML5 ``draggable=true`` so the
   *  user can drag it onto another store section. Only set in the
   *  grouped-view active list — completed items and the flat view
   *  stay non-draggable so a reorder doesn't suggest an unsupported
   *  meaning. */
  draggable: boolean
  onDragItemStart: ((id: string) => void) | null
  onDragItemEnd: (() => void) | null
}

function ItemRow(props: RowProps) {
  const { item, done } = props
  if (props.isEditing) return (
    <li class="sh-shopping-item sh-shopping-item--edit">
      <EditRow
        initialText={item.text}
        onSave={props.onEditSave}
        onCancel={props.onEditCancel}
      />
    </li>
  )

  return (
    <li
      class={'sh-shopping-item ' + (done ? 'sh-item--done' : '')}
      draggable={props.draggable}
      onDragStart={(e) => {
        if (!props.draggable) return
        e.dataTransfer?.setData(DRAG_ITEM_MIME, item.id)
        // Force the move cursor — Chrome's default ``copy`` would
        // suggest the wrong semantic (the item moves between stores,
        // not gets duplicated).
        if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move'
        props.onDragItemStart?.(item.id)
      }}
      onDragEnd={() => props.onDragItemEnd?.()}
    >
      {/* Standalone checkbox button — no ``<label>`` wrapping the row
       *  content. Clicking the box is the ONLY toggle action; clicks
       *  on the text / pill / delete never accidentally check the
       *  item off (the long-standing "I meant to edit but I marked
       *  it done" trap). */}
      <button
        type="button"
        class={
          'sh-shopping-item__check '
          + (done ? 'sh-shopping-item__check--done' : '')
        }
        onClick={props.onToggle}
        aria-label={
          done
            ? `Put ${item.text} back on the list`
            : `Mark ${item.text} as bought`
        }
        aria-pressed={done}
      >
        <span aria-hidden="true">{done ? '✓' : ''}</span>
      </button>
      <span
        class="sh-shopping-item__text"
        onClick={() => { if (!done) props.onEditStart() }}
        title={done ? '' : 'Click to rename'}
      >
        {item.text}
      </span>
      {!done && (
        <StorePicker
          currentStore={item.store ?? null}
          storeNames={props.storeNames}
          onPick={props.onReassignStore}
        />
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
  onSave,
  onCancel,
}: {
  initialText: string
  onSave: (text: string) => void
  onCancel: () => void
}) {
  const [text, setText] = useState(initialText)
  const textRef = useRef<HTMLInputElement | null>(null)
  useEffect(() => {
    textRef.current?.focus()
    textRef.current?.select()
  }, [])
  const submit = () => onSave(text)
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

// ─── Store picker popover (replaces the second text input in EditRow) ──

interface StorePickerProps {
  /** Current store on the item, or ``null`` for "No store". */
  currentStore: string | null
  /** Household catalogue. The picker also offers "+ New store…" to
   *  extend this list inline (no native ``prompt()`` — the
   *  popover swaps to a focused text input instead). */
  storeNames: string[]
  onPick: (next: string | null) => void
}

/** One-tap reassign affordance on the item row.
 *
 *  Replaces the typing-required second text input that used to live
 *  inside ``EditRow``: the user no longer has to remember the store
 *  name or enter rename mode at all to drop an item onto Migros. The
 *  button surface stays a pill so it reads as paired metadata at
 *  rest. The popover anchors under the pill on desktop and docks as
 *  a bottom-sheet on mobile.
 *
 *  Two-mode state machine inside the popover:
 *   - ``list`` (default): existing stores + "No store" + "+ New
 *     store…" call-to-action.
 *   - ``new``: the menu items are replaced by a focused text input
 *     ("Type a store name") + Save / "← Back". Submit creates the
 *     store *and* assigns the item to it in one round-trip via
 *     ``onPick(name)`` — the parent's ``updateItem`` already
 *     server-side upserts a new catalogue row whenever it sees an
 *     unknown store name. Replaces the previous ``window.prompt``
 *     call (poor mobile UX + the native dialog hid the picker). */
function StorePicker({ currentStore, storeNames, onPick }: StorePickerProps) {
  const [open, setOpen] = useState(false)
  /** Sub-view inside the popover. ``list`` is the default;
   *  ``new`` swaps to the inline name-entry input. */
  const [mode, setMode] = useState<'list' | 'new'>('list')
  const [newName, setNewName] = useState('')
  const ref = useRef<HTMLDivElement | null>(null)
  const newInputRef = useRef<HTMLInputElement | null>(null)

  // Click-outside closes the menu. ``useLayoutEffect`` would be
  // overkill — the menu is small enough that one async paint frame
  // of stale state is invisible.
  useEffect(() => {
    if (!open) return
    const onDocClick = (e: MouseEvent) => {
      if (!ref.current) return
      if (!ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        // Escape unwinds one step at a time: from ``new`` back to
        // ``list``, from ``list`` to closed. Mirrors the way a
        // native iOS / Android picker handles the back button.
        if (mode === 'new') {
          setMode('list')
          setNewName('')
        } else {
          setOpen(false)
        }
      }
    }
    document.addEventListener('mousedown', onDocClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDocClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, mode])

  // Reset to list mode whenever the picker closes — the next open
  // should always start from the store list, not from a stale
  // half-typed new-store name.
  useEffect(() => {
    if (!open) {
      setMode('list')
      setNewName('')
    }
  }, [open])

  // Autofocus the new-store input when we enter ``new`` mode so the
  // mobile keyboard pops up immediately.
  useEffect(() => {
    if (mode === 'new') {
      newInputRef.current?.focus()
    }
  }, [mode])

  const pick = (next: string | null) => {
    if (next !== currentStore) onPick(next)
    setOpen(false)
  }

  const saveNew = () => {
    const trimmed = newName.trim()
    if (!trimmed) return
    onPick(trimmed)
    setOpen(false)
  }

  const label = currentStore || 'Set store'

  return (
    <div class="sh-shopping-store-picker" ref={ref}>
      <button
        type="button"
        class={
          'sh-shopping-store-pill '
          + (currentStore ? '' : 'sh-shopping-store-pill--empty')
        }
        aria-haspopup="menu"
        aria-expanded={open}
        title={currentStore ? `At ${currentStore} — tap to change` : 'Tap to set a store'}
        onClick={(e) => {
          e.stopPropagation()
          setOpen((v) => !v)
        }}
      >
        <span aria-hidden="true">📍</span>
        <span>{label}</span>
        <span aria-hidden="true" class="sh-shopping-store-pill__chev">▾</span>
      </button>
      {open && mode === 'list' && (
        <ul class="sh-shopping-store-picker__menu" role="menu">
          {storeNames.length === 0 && (
            <li class="sh-shopping-store-picker__empty" role="presentation">
              No stores yet — tap below to add one.
            </li>
          )}
          {storeNames.map((name) => (
            <li key={name} role="none">
              <button
                type="button"
                role="menuitem"
                class={
                  'sh-shopping-store-picker__opt '
                  + (currentStore === name ? 'sh-shopping-store-picker__opt--current' : '')
                }
                onClick={() => pick(name)}
              >
                {name}
                {currentStore === name && (
                  <span aria-hidden="true" class="sh-shopping-store-picker__check">✓</span>
                )}
              </button>
            </li>
          ))}
          <li role="separator" class="sh-shopping-store-picker__sep" />
          <li role="none">
            <button
              type="button"
              role="menuitem"
              class={
                'sh-shopping-store-picker__opt '
                + (currentStore === null ? 'sh-shopping-store-picker__opt--current' : '')
              }
              onClick={() => pick(null)}
            >
              No store
              {currentStore === null && (
                <span aria-hidden="true" class="sh-shopping-store-picker__check">✓</span>
              )}
            </button>
          </li>
          <li role="none">
            <button
              type="button"
              role="menuitem"
              class="sh-shopping-store-picker__opt sh-shopping-store-picker__opt--new"
              onClick={() => setMode('new')}
            >
              + New store…
            </button>
          </li>
        </ul>
      )}
      {open && mode === 'new' && (
        <div class="sh-shopping-store-picker__menu sh-shopping-store-picker__new" role="dialog" aria-label="Add a new store">
          <button
            type="button"
            class="sh-shopping-store-picker__back"
            onClick={() => { setMode('list'); setNewName('') }}
          >
            ‹ Back
          </button>
          <input
            ref={newInputRef}
            type="text"
            class="sh-shopping-store-picker__new-input"
            placeholder="Store name (e.g. Migros)"
            aria-label="New store name"
            value={newName}
            onInput={(e) => setNewName((e.target as HTMLInputElement).value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                saveNew()
              }
            }}
          />
          <Button
            type="button"
            onClick={saveNew}
            disabled={!newName.trim()}
          >
            Save
          </Button>
        </div>
      )}
    </div>
  )
}
