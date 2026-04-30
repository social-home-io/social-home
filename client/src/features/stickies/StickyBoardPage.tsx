/**
 * StickyBoardPage — draggable sticky-note board (§19 / §23.24 / §23.61).
 *
 * Free-position board: each sticky has ``position_x`` / ``position_y``
 * (0–1000 normalised to board width). Drag with mouse/touch to move;
 * the new position is PATCHed to the backend on release. All CRUD goes
 * through the shared ``stickies`` store so WS frames from other tabs /
 * co-members merge in live.
 *
 * Pass ``spaceId`` for the per-space board variant (``/api/spaces/{id}
 * /stickies``); omit for the household board.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { stickies, type StickyRow } from '@/store/stickies'

const COLORS = ['#FFF9B1', '#FFB3B3', '#B3FFB3', '#B3D4FF', '#E8B3FF', '#FFD4B3']

const BOARD_W = 1000   // normalised coordinate space (width units)
const BOARD_H = 700    // normalised coordinate space (height units)

const loading = signal(true)

export interface StickyBoardPageProps {
  spaceId?: string
}

function base(spaceId?: string): string {
  return spaceId
    ? `/api/spaces/${spaceId}/stickies`
    : `/api/stickies`
}

export default function StickyBoardPage({ spaceId }: StickyBoardPageProps) {
  useTitle('Sticky notes')
  const boardRef = useRef<HTMLDivElement | null>(null)
  const [dragging, setDragging] = useState<string | null>(null)

  useEffect(() => {
    loading.value = true
    api.get(base(spaceId)).then((rows: StickyRow[]) => {
      stickies.value = rows
      loading.value = false
    }).catch(() => {
      loading.value = false
      stickies.value = []
    })
  }, [spaceId])

  const addSticky = async () => {
    const content = prompt('Sticky note:')
    if (!content?.trim()) return
    const color = COLORS[stickies.value.length % COLORS.length]
    // Random initial position in the middle third of the board so new
    // stickies don't stack on top of each other.
    const position_x = 120 + Math.random() * (BOARD_W - 320)
    const position_y = 80 + Math.random() * (BOARD_H - 280)
    try {
      const row = await api.post(base(spaceId), {
        content, color, position_x, position_y,
      }) as StickyRow
      if (!stickies.value.some(s => s.id === row.id)) {
        stickies.value = [...stickies.value, row]
      }
    } catch (err: unknown) {
      showToast(`Add failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  const updateContent = async (id: string) => {
    const sticky = stickies.value.find(s => s.id === id)
    if (!sticky) return
    const content = prompt('Edit note:', sticky.content)
    if (content === null) return
    try {
      const updated = await api.patch(
        `${base(spaceId)}/${id}`, { content },
      ) as StickyRow
      stickies.value = stickies.value.map(s => s.id === id ? updated : s)
    } catch (err: unknown) {
      showToast(`Update failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  const deleteSticky = async (id: string) => {
    if (!confirm('Delete this sticky?')) return
    try {
      await api.delete(`${base(spaceId)}/${id}`)
      stickies.value = stickies.value.filter(s => s.id !== id)
    } catch (err: unknown) {
      showToast(`Delete failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  /** Pointer-drag — live-update local position on move, PATCH on up. */
  const onPointerDown = (e: PointerEvent, sticky: StickyRow) => {
    const el = e.currentTarget as HTMLElement
    const board = boardRef.current
    if (!board) return
    el.setPointerCapture(e.pointerId)
    setDragging(sticky.id)
    const rect = board.getBoundingClientRect()
    const scaleX = BOARD_W / rect.width
    const scaleY = BOARD_H / rect.height
    // Offset between pointer and sticky's top-left in scaled coords.
    const offsetX = (e.clientX - rect.left) * scaleX - sticky.position_x
    const offsetY = (e.clientY - rect.top)  * scaleY - sticky.position_y

    const onMove = (ev: PointerEvent) => {
      const x = Math.max(0, Math.min(
        BOARD_W - 180,
        (ev.clientX - rect.left) * scaleX - offsetX,
      ))
      const y = Math.max(0, Math.min(
        BOARD_H - 140,
        (ev.clientY - rect.top) * scaleY - offsetY,
      ))
      stickies.value = stickies.value.map(s =>
        s.id === sticky.id ? { ...s, position_x: x, position_y: y } : s,
      )
    }
    const onUp = async (ev: PointerEvent) => {
      el.releasePointerCapture(ev.pointerId)
      el.removeEventListener('pointermove', onMove)
      el.removeEventListener('pointerup', onUp)
      el.removeEventListener('pointercancel', onUp)
      setDragging(null)
      const cur = stickies.value.find(s => s.id === sticky.id)
      if (!cur) return
      // Only PATCH if we actually moved.
      if (cur.position_x === sticky.position_x &&
          cur.position_y === sticky.position_y) return
      try {
        await api.patch(`${base(spaceId)}/${sticky.id}`, {
          position_x: cur.position_x,
          position_y: cur.position_y,
        })
      } catch (err: unknown) {
        showToast(`Move failed: ${(err as Error)?.message ?? err}`, 'error')
      }
    }
    el.addEventListener('pointermove', onMove)
    el.addEventListener('pointerup', onUp)
    el.addEventListener('pointercancel', onUp)
  }

  if (loading.value) return <Spinner />

  return (
    <div class="sh-sticky-board">
      <div class="sh-page-header">
        <Button onClick={addSticky}>+ Add sticky</Button>
      </div>
      {stickies.value.length === 0 ? (
        <div class="sh-empty-state">
          <div style={{ fontSize: '2rem' }}>📝</div>
          <h3>The board is empty</h3>
          <p>Stickies are quick shared notes — reminders, lists, thoughts.
             Pin one for the whole household.</p>
          <div style={{ marginTop: '0.75rem' }}>
            <Button onClick={addSticky}>+ Add your first sticky</Button>
          </div>
        </div>
      ) : (
        <div
          ref={boardRef}
          class="sh-sticky-canvas"
          style={{ aspectRatio: `${BOARD_W} / ${BOARD_H}` }}
        >
          {stickies.value.map(s => (
            <div
              key={s.id}
              class={`sh-sticky ${dragging === s.id ? 'sh-sticky--dragging' : ''}`}
              style={{
                background: s.color,
                left:   `${(s.position_x / BOARD_W) * 100}%`,
                top:    `${(s.position_y / BOARD_H) * 100}%`,
                width:  '180px',
                height: '140px',
              }}
              onPointerDown={(e) => onPointerDown(e, s)}
            >
              <button
                type="button" class="sh-sticky-content"
                aria-label={`Edit sticky: ${s.content}`}
                onClick={() => void updateContent(s.id)}
                onPointerDown={(e) => e.stopPropagation()}
              >
                {s.content}
              </button>
              <button
                type="button" class="sh-sticky-delete"
                aria-label="Delete sticky note"
                onClick={() => void deleteSticky(s.id)}
                onPointerDown={(e) => e.stopPropagation()}
              >✕</button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
