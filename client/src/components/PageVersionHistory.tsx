/**
 * PageVersionHistory — version list + diff + revert (§23.14, §23.36).
 *
 * Hits `GET /api/pages/{id}/versions` and `POST /api/pages/{id}/revert`.
 * The diff view shows a simple line-level added/removed presentation —
 * good enough for household scale; swap in a real diff library later if
 * pages get long.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Button } from './Button'
import { ConfirmDialog } from './ConfirmDialog'
import { Spinner } from './Spinner'
import { showToast } from './Toast'

interface Version {
  id:              string
  page_id:         string
  version:         number
  title:           string
  content:         string
  edited_by:       string
  edited_at:       string
}

const versions        = signal<Version[]>([])
const loading         = signal(false)
const showRevert      = signal<Version | null>(null)
const activeDiff      = signal<Version | null>(null)
const currentContent  = signal<string>('')

export async function loadVersions(pageId: string, liveContent = '') {
  loading.value = true
  currentContent.value = liveContent
  try {
    const rows = await api.get(`/api/pages/${pageId}/versions`) as Version[]
    // Newest first — backend sorts by version asc.
    versions.value = [...rows].sort((a, b) => b.version - a.version)
  } catch (err: any) {
    showToast(`Could not load versions: ${err?.message || err}`, 'error')
    versions.value = []
  } finally {
    loading.value = false
  }
}

async function doRevert(pageId: string, version: number, onRevert?: (v: number) => void) {
  try {
    await api.post(`/api/pages/${pageId}/revert`, { version })
    showToast('Reverted', 'success')
    onRevert?.(version)
    await loadVersions(pageId, currentContent.value)
  } catch (err: any) {
    showToast(`Revert failed: ${err?.message || err}`, 'error')
  }
}

function simpleDiff(a: string, b: string): Array<['+'|'-'|' ', string]> {
  // Line-level diff: anything in ``b`` but not ``a`` is added; reverse is removed.
  // The household use-case rarely cares about granular word diffs.
  const aLines = (a || '').split('\n')
  const bLines = (b || '').split('\n')
  const aSet = new Set(aLines)
  const bSet = new Set(bLines)
  const out: Array<['+'|'-'|' ', string]> = []
  for (const line of aLines) {
    out.push([bSet.has(line) ? ' ' : '-', line])
  }
  for (const line of bLines) {
    if (!aSet.has(line)) out.push(['+', line])
  }
  return out
}

export function PageVersionHistory({ pageId, onRevert }: {
  pageId: string; onRevert?: (version: number) => void
}) {
  return (
    <div class="sh-versions">
      <h3>Version History</h3>
      {loading.value && <Spinner />}
      {!loading.value && versions.value.length === 0 && (
        <p class="sh-muted">No previous versions — edits will appear here.</p>
      )}
      {versions.value.map(v => (
        <div key={v.id} class="sh-version-row">
          <div class="sh-row sh-justify-between">
            <span>v{v.version} — {v.edited_by}</span>
            <time class="sh-muted">
              {new Date(v.edited_at).toLocaleString()}
            </time>
          </div>
          <div class="sh-row">
            <Button
              variant="secondary"
              onClick={() => (activeDiff.value = activeDiff.value?.id === v.id ? null : v)}
            >
              {activeDiff.value?.id === v.id ? 'Hide diff' : 'Show diff'}
            </Button>
            <Button variant="secondary" onClick={() => (showRevert.value = v)}>
              Revert
            </Button>
          </div>
          {activeDiff.value?.id === v.id && (
            <pre class="sh-diff">
              {simpleDiff(v.content, currentContent.value).map(([sign, line], i) => (
                <div
                  key={i}
                  class={
                    sign === '+' ? 'sh-diff-add'
                      : sign === '-' ? 'sh-diff-del'
                      : 'sh-diff-same'
                  }
                >
                  <span class="sh-diff-sign">{sign}</span>
                  <span>{line}</span>
                </div>
              ))}
            </pre>
          )}
        </div>
      ))}
      <ConfirmDialog
        open={showRevert.value !== null}
        title="Revert to this version?"
        message="The current content will be replaced. The current text is kept in history — you can revert again."
        onConfirm={async () => {
          const target = showRevert.value
          showRevert.value = null
          if (target) await doRevert(pageId, target.version, onRevert)
        }}
        onCancel={() => (showRevert.value = null)}
      />
    </div>
  )
}
