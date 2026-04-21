/**
 * PagesPage — household Markdown wiki (§23.58 / §23.72).
 *
 * Viewer:
 *   - Renders Markdown via {@link MarkdownView} with an auto-generated
 *     sticky table of contents on desktop.
 *   - Shows a "Last edited by … · <when>" strip under the title.
 *   - "History" button opens the version drawer with inline diff.
 *   - "Edit" button disabled while another user holds the lock.
 *
 * Editor:
 *   - Inline title input; auto-saves 1 s after last keystroke.
 *   - Split-pane Markdown source (left) + live preview (right) on
 *     desktop; tab-toggle on mobile.
 *   - Two-row toolbar with keyboard shortcuts (see MarkdownToolbar).
 *   - Autosave fires 2 s after last keystroke; status pill shows
 *     "Saving… / Saved ✓ / Save failed" feedback.
 *   - Acquires an edit lock on mount + posts ``/lock/refresh`` every
 *     30 s; releases on unmount.
 *   - Conflict UI (409 on save): shows rendered Markdown on both
 *     sides + "Keep mine / Keep theirs / Merge manually" actions.
 *
 * List:
 *   - "New page" opens {@link NewPageDialog}; empty-state surfaces a
 *     friendly CTA + Markdown syntax help card.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import { Button } from '@/components/Button'
import {
  MarkdownToolbar,
  useMarkdownShortcuts,
} from '@/components/MarkdownToolbar'
import { MarkdownView } from '@/components/MarkdownView'
import { NewPageDialog } from '@/components/NewPageDialog'
import { PageHistoryDrawer } from '@/components/PageHistoryDrawer'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { extractHeadings } from '@/utils/markdown'
import type { EditLock, Page } from '@/types'

interface ConflictData {
  mine: string
  theirs: string
  theirs_by: string
  base_updated_at: string
}

const pages        = signal<Page[]>([])
const viewing      = signal<Page | null>(null)
const editing      = signal(false)
const editContent  = signal('')
const editTitle    = signal('')
const loading      = signal(true)
const editLock     = signal<EditLock | null>(null)
const conflict     = signal<ConflictData | null>(null)
const showNew      = signal(false)
const showHistory  = signal(false)
const mobileView   = signal<'edit' | 'preview'>('edit')

// Autosave state (outside hooks so it survives across renders of the
// same editor mount without re-creating the debounce timer).
type SaveStatus = 'idle' | 'saving' | 'saved' | 'error'
const saveStatus = signal<SaveStatus>('idle')
const lastSavedAt = signal<string | null>(null)

export default function PagesPage() {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const saveTimer = useRef<number | null>(null)
  const titleSaveTimer = useRef<number | null>(null)
  const heartbeatTimer = useRef<number | null>(null)

  useEffect(() => {
    void api.get('/api/pages').then((rows: Page[]) => {
      pages.value = rows
      loading.value = false
    })

    const offLock = ws.on('page.editing', (evt) => {
      const data = evt.data as unknown as EditLock & { page_id: string }
      if (viewing.value && data.page_id === viewing.value.id) {
        editLock.value = {
          locked_by: data.locked_by,
          locked_at: data.locked_at ?? null,
          lock_expires_at: data.lock_expires_at ?? null,
        }
      }
    })
    const offUnlock = ws.on('page.editing_done', (evt) => {
      const data = evt.data as { page_id: string }
      if (viewing.value?.id === data.page_id) editLock.value = null
    })
    const offConflict = ws.on('page.conflict', (evt) => {
      const data = evt.data as {
        page_id: string; theirs: string; theirs_by: string;
      }
      if (
        viewing.value?.id === data.page_id &&
        editing.value &&
        !conflict.value
      ) {
        conflict.value = {
          mine:            editContent.value,
          theirs:          data.theirs,
          theirs_by:       data.theirs_by,
          base_updated_at: viewing.value.updated_at,
        }
      }
    })

    return () => { offLock(); offUnlock(); offConflict() }
  }, [])

  useMarkdownShortcuts(textareaRef, (s) => {
    editContent.value = s
    scheduleAutosave()
  })

  // ─── Autosave ────────────────────────────────────────────────────────

  const doSave = async () => {
    if (!viewing.value || !editing.value) return
    if (conflict.value) return    // don't thrash while resolving
    saveStatus.value = 'saving'
    try {
      const updated = await api.patch(
        `/api/pages/${viewing.value.id}`,
        {
          content: editContent.value,
          base_updated_at: viewing.value.updated_at,
        },
      ) as Page
      viewing.value = updated
      pages.value = pages.value.map(p => p.id === updated.id ? updated : p)
      lastSavedAt.value = updated.updated_at
      saveStatus.value = 'saved'
      window.setTimeout(() => {
        if (saveStatus.value === 'saved') saveStatus.value = 'idle'
      }, 2000)
    } catch (err: unknown) {
      const msg = (err as Error)?.message ?? String(err)
      if (msg.includes('409') || msg.toLowerCase().includes('stale')) {
        await surfaceConflict()
      } else {
        saveStatus.value = 'error'
        showToast(`Save failed: ${msg}`, 'error')
      }
    }
  }

  const scheduleAutosave = () => {
    if (saveTimer.current !== null) window.clearTimeout(saveTimer.current)
    saveTimer.current = window.setTimeout(() => {
      saveTimer.current = null
      void doSave()
    }, 2000)
  }

  const surfaceConflict = async () => {
    if (!viewing.value) return
    try {
      const latest = await api.get(`/api/pages/${viewing.value.id}`) as Page
      conflict.value = {
        mine:            editContent.value,
        theirs:          latest.content,
        theirs_by:       latest.last_editor_user_id ?? 'another user',
        base_updated_at: latest.updated_at,
      }
      viewing.value = latest
    } catch {
      saveStatus.value = 'error'
    }
  }

  // ─── Title autosave (inline title input in editor) ───────────────────

  const scheduleTitleSave = () => {
    if (titleSaveTimer.current !== null) {
      window.clearTimeout(titleSaveTimer.current)
    }
    titleSaveTimer.current = window.setTimeout(async () => {
      titleSaveTimer.current = null
      if (!viewing.value) return
      const t = editTitle.value.trim()
      if (!t || t === viewing.value.title) return
      try {
        const updated = await api.patch(
          `/api/pages/${viewing.value.id}`,
          { title: t, base_updated_at: viewing.value.updated_at },
        ) as Page
        viewing.value = updated
        pages.value = pages.value.map(p => p.id === updated.id ? updated : p)
      } catch (err: unknown) {
        showToast(
          `Rename failed: ${(err as Error)?.message ?? err}`,
          'error',
        )
      }
    }, 1000)
  }

  // ─── Lock lifecycle ──────────────────────────────────────────────────

  const acquireAndHeartbeat = async (pageId: string) => {
    try {
      await api.post(`/api/pages/${pageId}/lock`, {})
    } catch { /* best-effort — another editor may already hold it */ }
    heartbeatTimer.current = window.setInterval(() => {
      api.post(`/api/pages/${pageId}/lock/refresh`, {}).catch(() => {})
    }, 30_000)
  }

  const releaseLock = (pageId: string) => {
    if (heartbeatTimer.current !== null) {
      window.clearInterval(heartbeatTimer.current)
      heartbeatTimer.current = null
    }
    api.delete(`/api/pages/${pageId}/lock`).catch(() => {})
  }

  // ─── Flow actions ────────────────────────────────────────────────────

  const createPage = async (title: string) => {
    const page = await api.post('/api/pages', { title, content: '' }) as Page
    pages.value = [page, ...pages.value]
    viewing.value = page
    editContent.value = ''
    editTitle.value = page.title
    editing.value = true
    saveStatus.value = 'idle'
    showNew.value = false
    void acquireAndHeartbeat(page.id)
  }

  const viewPage = async (id: string) => {
    const page = await api.get(`/api/pages/${id}`) as Page
    viewing.value = page
    editing.value = false
    editLock.value = null
    conflict.value = null
    showHistory.value = false
    try {
      const lock = await api.get(`/api/pages/${id}/lock`) as EditLock | null
      if (lock) editLock.value = lock
    } catch { /* noop */ }
  }

  const startEditing = async () => {
    if (!viewing.value) return
    editContent.value = viewing.value.content
    editTitle.value   = viewing.value.title
    editing.value     = true
    conflict.value    = null
    saveStatus.value  = 'idle'
    void acquireAndHeartbeat(viewing.value.id)
  }

  const stopEditing = (opts: { save?: boolean } = {}) => {
    if (!viewing.value) return
    if (opts.save) void doSave()
    releaseLock(viewing.value.id)
    editing.value = false
    saveStatus.value = 'idle'
    if (saveTimer.current !== null) {
      window.clearTimeout(saveTimer.current)
      saveTimer.current = null
    }
    if (titleSaveTimer.current !== null) {
      window.clearTimeout(titleSaveTimer.current)
      titleSaveTimer.current = null
    }
  }

  const deletePage = async () => {
    if (!viewing.value) return
    if (!confirm(`Delete "${viewing.value.title}"?`)) return
    try {
      await api.delete(`/api/pages/${viewing.value.id}`)
      pages.value = pages.value.filter(p => p.id !== viewing.value!.id)
      viewing.value = null
      showToast('Page deleted', 'info')
    } catch (err: unknown) {
      showToast(
        `Delete failed: ${(err as Error)?.message ?? err}`,
        'error',
      )
    }
  }

  const resolveConflict = (choice: 'mine' | 'theirs' | 'merge') => {
    if (!conflict.value) return
    if (choice === 'mine') {
      editContent.value = conflict.value.mine
    } else if (choice === 'theirs') {
      editContent.value = conflict.value.theirs
    } else {
      editContent.value =
        '<<<<<<< your changes\n' +
        conflict.value.mine +
        '\n=======\n' +
        conflict.value.theirs +
        `\n>>>>>>> ${conflict.value.theirs_by}\n`
    }
    conflict.value = null
    showToast(
      choice === 'mine'    ? 'Keeping your version — save to overwrite' :
      choice === 'theirs'  ? 'Switched to their version' :
                             'Ready to merge manually',
      'info',
    )
  }

  if (loading.value) return <Spinner />

  // ─── Render ──────────────────────────────────────────────────────────

  // 1. Conflict modal takes precedence over everything else.
  if (viewing.value && editing.value && conflict.value) {
    return (
      <div class="sh-page-conflict">
        <div class="sh-page-header">
          <h1>Conflict: {viewing.value.title}</h1>
        </div>
        <p class="sh-conflict-banner">
          <strong>{conflict.value.theirs_by}</strong> edited this page while
          you were editing. Choose which version to keep, or merge them by
          hand.
        </p>
        <div class="sh-conflict-panels">
          <div class="sh-conflict-panel">
            <h3>Your version</h3>
            <MarkdownView src={conflict.value.mine} />
            <Button onClick={() => resolveConflict('mine')}>Keep mine</Button>
          </div>
          <div class="sh-conflict-panel">
            <h3>Their version</h3>
            <MarkdownView src={conflict.value.theirs} />
            <Button onClick={() => resolveConflict('theirs')}>Keep theirs</Button>
          </div>
        </div>
        <div class="sh-form-actions">
          <Button variant="secondary" onClick={() => resolveConflict('merge')}>
            Merge manually
          </Button>
        </div>
      </div>
    )
  }

  // 2. Editor.
  if (viewing.value && editing.value) {
    const status = saveStatus.value
    const statusLabel =
      status === 'saving' ? 'Saving…' :
      status === 'saved'  ? 'Saved ✓' :
      status === 'error'  ? 'Save failed — retry' : ''
    return (
      <div class="sh-page-editor">
        <div class="sh-page-header">
          <input
            class="sh-page-editor-title"
            aria-label="Page title"
            value={editTitle.value}
            onInput={(e) => {
              editTitle.value = (e.target as HTMLInputElement).value
              scheduleTitleSave()
            }}
          />
          <div class="sh-row">
            {statusLabel && (
              <span
                class={`sh-page-editor-status sh-page-editor-status--${status}`}
                role="status"
                onClick={() => status === 'error' && void doSave()}
              >
                {statusLabel}
              </span>
            )}
            <Button variant="secondary" onClick={() => stopEditing()}>Close</Button>
            <Button onClick={() => stopEditing({ save: true })}>Save & close</Button>
          </div>
        </div>

        <MarkdownToolbar
          textareaRef={textareaRef}
          onUpdate={(s) => { editContent.value = s; scheduleAutosave() }}
        />

        <div class="sh-page-editor-mobile-tabs" role="tablist">
          <button
            type="button" role="tab"
            aria-pressed={mobileView.value === 'edit'}
            onClick={() => { mobileView.value = 'edit' }}
          >Edit</button>
          <button
            type="button" role="tab"
            aria-pressed={mobileView.value === 'preview'}
            onClick={() => { mobileView.value = 'preview' }}
          >Preview</button>
        </div>

        <div class="sh-page-editor-panes">
          <textarea
            ref={textareaRef}
            value={editContent.value}
            class={mobileView.value === 'preview' ? 'sh-page-editor-hidden-mobile' : ''}
            onInput={(e) => {
              editContent.value = (e.target as HTMLTextAreaElement).value
              scheduleAutosave()
            }}
            aria-label="Markdown source"
            placeholder={'# Heading\n\nWrite your page here. Markdown is supported.'}
          />
          <div
            class={`sh-page-editor-preview ${mobileView.value === 'edit' ? 'sh-page-editor-hidden-mobile' : ''}`}
            aria-label="Live preview"
          >
            <MarkdownView src={editContent.value} live />
          </div>
        </div>
      </div>
    )
  }

  // 3. Viewer.
  if (viewing.value) {
    const page = viewing.value
    const headings = extractHeadings(page.content)
    const editorLabel = page.last_editor_user_id || page.created_by
    const editedAt = page.last_edited_at || page.updated_at
    return (
      <>
        <div class="sh-page-viewer">
          <div class="sh-page-header">
            <div>
              <h1 style={{ margin: 0 }}>{page.title}</h1>
              <div class="sh-muted" style={{ fontSize: '0.875rem' }}>
                Edited by <strong>{editorLabel}</strong> ·{' '}
                <time>{new Date(editedAt).toLocaleString()}</time>
              </div>
            </div>
            <div class="sh-row">
              <Button
                variant="secondary"
                onClick={() => { viewing.value = null; editLock.value = null }}
              >Back</Button>
              <Button variant="secondary" onClick={() => showHistory.value = true}>
                History
              </Button>
              <Button onClick={startEditing} disabled={!!editLock.value}>
                Edit
              </Button>
              <Button variant="danger" onClick={() => void deletePage()}>
                Delete
              </Button>
            </div>
          </div>
          {editLock.value && (
            <div class="sh-edit-lock-banner" role="alert">
              🔒 <strong>{editLock.value.locked_by}</strong> is currently
              editing this page.
            </div>
          )}
          <div class="sh-page-viewer-layout">
            {page.content
              ? <MarkdownView src={page.content} />
              : (
                <div class="sh-empty-state">
                  <p>This page is empty.</p>
                  <p>Click <strong>Edit</strong> to add content.</p>
                </div>
              )}
            {headings.length > 0 && (
              <nav class="sh-page-toc" aria-label="Table of contents">
                <h4>On this page</h4>
                <ul>
                  {headings.map(h => (
                    <li key={h.slug} class={`sh-toc-depth-${h.depth}`}>
                      <a href={`#${h.slug}`}>{h.text}</a>
                    </li>
                  ))}
                </ul>
              </nav>
            )}
          </div>
        </div>
        <PageHistoryDrawer
          pageId={page.id}
          currentContent={page.content}
          open={showHistory.value}
          onClose={() => showHistory.value = false}
          onRestored={(c) => {
            if (viewing.value) viewing.value = { ...viewing.value, content: c }
            void viewPage(page.id)
          }}
        />
      </>
    )
  }

  // 4. Index.
  return (
    <div class="sh-pages">
      <div class="sh-page-header">
        <h1>Pages</h1>
        <Button onClick={() => showNew.value = true}>+ New page</Button>
      </div>
      {pages.value.length === 0 ? (
        <div class="sh-empty-state">
          <h3 style={{ margin: 0 }}>No pages yet</h3>
          <p>Pages are shared Markdown documents for your household —
             notes, plans, recipes, anything you want to keep in one place.</p>
          <div style={{ marginTop: '0.75rem' }}>
            <Button onClick={() => showNew.value = true}>
              + Create your first page
            </Button>
          </div>
          <details style={{ marginTop: '1rem', textAlign: 'left' }}>
            <summary class="sh-muted">
              Markdown basics
            </summary>
            <ul style={{ marginTop: '0.5rem', paddingLeft: '1.25rem' }}>
              <li><code># Heading</code> → H1</li>
              <li><code>**bold**</code> → <strong>bold</strong></li>
              <li><code>*italic*</code> → <em>italic</em></li>
              <li><code>[link](https://…)</code> — links to a URL</li>
              <li><code>- item</code> or <code>1. item</code> — lists</li>
              <li><code>![alt](url)</code> — embed an image</li>
              <li><code>[[Page Title]]</code> — link to another page</li>
            </ul>
          </details>
        </div>
      ) : pages.value.map(p => (
        <div key={p.id} class="sh-page-card" onClick={() => void viewPage(p.id)}>
          <div>
            <strong>{p.title}</strong>
            {p.last_editor_user_id && (
              <div class="sh-muted" style={{ fontSize: '0.75rem' }}>
                Edited by {p.last_editor_user_id}
              </div>
            )}
          </div>
          <time>{new Date(p.updated_at).toLocaleString()}</time>
        </div>
      ))}

      <NewPageDialog
        open={showNew.value}
        onCancel={() => showNew.value = false}
        onCreate={(t) => void createPage(t)}
      />
    </div>
  )
}
