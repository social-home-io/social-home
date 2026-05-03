/**
 * PageHistoryDrawer — right-side drawer showing edit history + diff.
 *
 * Fetches ``GET /api/pages/{id}/versions`` (newest first after sort),
 * renders a per-entry card with editor + timestamp, and shows an inline
 * line-level diff between the selected version and the current page
 * body. Admins get a "Restore" button that POSTs ``/revert``.
 *
 * The diff is a small local LCS — O(n·m) on line counts. Fine for
 * Markdown pages (spec §2627 caps body at 4000 chars, so < ~100 lines).
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { currentUser } from '@/store/auth'
import { Button } from './Button'
import { showToast } from './Toast'
import type { PageVersion } from '@/types'
import { confirmDialog } from '@/components/confirm'

interface Props {
  pageId: string
  currentContent: string
  open: boolean
  onClose: () => void
  /** Called after a successful revert so the parent can reload the page. */
  onRestored: (newContent: string) => void
}

type DiffLine = { kind: 'same' | 'add' | 'del', text: string }

/** Tiny LCS-based line diff — not byte-perfect but good enough for
 * reviewing a Markdown page. Returns rows with {kind, text}. */
function diffLines(a: string, b: string): DiffLine[] {
  const A = a.split('\n')
  const B = b.split('\n')
  const m = A.length, n = B.length
  // LCS table
  const L: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0))
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1])
    }
  }
  const out: DiffLine[] = []
  let i = 0, j = 0
  while (i < m && j < n) {
    if (A[i] === B[j]) { out.push({ kind: 'same', text: A[i] }); i++; j++ }
    else if (L[i + 1][j] >= L[i][j + 1]) { out.push({ kind: 'del', text: A[i] }); i++ }
    else { out.push({ kind: 'add', text: B[j] }); j++ }
  }
  while (i < m) { out.push({ kind: 'del', text: A[i++] }) }
  while (j < n) { out.push({ kind: 'add', text: B[j++] }) }
  return out
}

export function PageHistoryDrawer(
  { pageId, currentContent, open, onClose, onRestored }: Props,
) {
  const [versions, setVersions] = useState<PageVersion[]>([])
  const [selected, setSelected] = useState<PageVersion | null>(null)
  const [busy, setBusy] = useState(false)
  const isAdmin = currentUser.value?.is_admin ?? false

  useEffect(() => {
    if (!open) return
    void api.get(`/api/pages/${pageId}/versions`).then((rows: PageVersion[]) => {
      const sorted = [...rows].sort((x, y) => y.version - x.version)
      setVersions(sorted)
      setSelected(sorted[0] ?? null)
    }).catch(() => {
      showToast('Could not load history', 'error')
    })
  }, [open, pageId])

  if (!open) return null

  const restore = async () => {
    if (!selected || busy) return
    if (!await confirmDialog(`Restore version ${selected.version}? The current body will be snapshotted first.`, { destructive: true })) return
    setBusy(true)
    try {
      const resp = await api.post(
        `/api/pages/${pageId}/revert`, { version: selected.version },
      ) as { content: string }
      showToast(`Restored to version ${selected.version}`, 'success')
      onRestored(resp.content ?? selected.content)
      onClose()
    } catch (err: unknown) {
      showToast(`Restore failed: ${(err as Error)?.message ?? err}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  const rows = selected ? diffLines(selected.content, currentContent) : []

  return (
    <aside class="sh-history-drawer" role="dialog" aria-label="Edit history">
      <div class="sh-history-drawer-header">
        <h3 style={{ margin: 0 }}>Edit history</h3>
        <button
          type="button" class="sh-modal-close"
          aria-label="Close history" onClick={onClose}
        >×</button>
      </div>
      <div class="sh-history-drawer-body">
        {versions.length === 0 && (
          <p class="sh-muted">No prior edits. Versions appear here after the first save.</p>
        )}
        {versions.map(v => (
          <div
            key={v.id}
            class={`sh-history-entry ${selected?.id === v.id ? 'sh-history-entry--active' : ''}`}
            onClick={() => setSelected(v)}
          >
            <div>
              <strong>v{v.version}</strong>
              {' '}
              <span class="sh-muted">{v.title}</span>
            </div>
            <div class="sh-history-entry-meta">
              <span>{v.edited_by}</span>
              <span>·</span>
              <span>{new Date(v.edited_at).toLocaleString()}</span>
            </div>
          </div>
        ))}

        {selected && (
          <>
            <h4 style={{ marginTop: '1rem' }}>Diff: v{selected.version} → current</h4>
            <div class="sh-history-diff" aria-label="Version diff">
              {rows.length === 0 && <em class="sh-muted">No changes.</em>}
              {rows.map((r, idx) => (
                <div
                  key={idx}
                  class={r.kind === 'add' ? 'sh-diff-add' : r.kind === 'del' ? 'sh-diff-del' : 'sh-diff-same'}
                >
                  {r.kind === 'add' ? '+ ' : r.kind === 'del' ? '- ' : '  '}
                  {r.text || '\u00A0'}
                </div>
              ))}
            </div>
            {isAdmin && (
              <div class="sh-form-actions" style={{ marginTop: '0.75rem' }}>
                <Button variant="primary" loading={busy} onClick={restore}>
                  Restore this version
                </Button>
              </div>
            )}
            {!isAdmin && (
              <p class="sh-muted" style={{ marginTop: '0.75rem' }}>
                Only household admins can restore an older version.
              </p>
            )}
          </>
        )}
      </div>
    </aside>
  )
}
