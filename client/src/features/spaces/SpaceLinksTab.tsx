/**
 * SpaceLinksTab — admin editor for the sidebar quick-links.
 *
 * Lists every configured link, lets owners/admins add new ones,
 * edit in place, reorder (via the position field), and delete.
 * Members see the links rendered in the space hero but can't edit
 * them here (the tab is hidden for non-admins by SpaceSettingsPage).
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'

interface SpaceLink {
  id: string
  label: string
  url: string
  position: number
}

interface Props {
  spaceId: string
}

export function SpaceLinksTab({ spaceId }: Props) {
  const [links, setLinks] = useState<SpaceLink[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [draft, setDraft] = useState<{ label: string; url: string }>({
    label: '',
    url: '',
  })

  const reload = async () => {
    setLoading(true)
    try {
      const body = await api.get(`/api/spaces/${spaceId}/links`) as {
        links: SpaceLink[]
      }
      setLinks(body.links)
    } catch (err: unknown) {
      showToast(`Failed to load links: ${(err as Error).message}`, 'error')
      setLinks([])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void reload() }, [spaceId])

  const createLink = async (e: Event) => {
    e.preventDefault()
    const label = draft.label.trim()
    const url = draft.url.trim()
    if (!label || !url) {
      showToast('Label and URL are required.', 'error')
      return
    }
    setSaving(true)
    try {
      await api.post(`/api/spaces/${spaceId}/links`, {
        label,
        url,
        position: links.length,
      })
      setDraft({ label: '', url: '' })
      await reload()
      showToast('Link added.', 'success')
    } catch (err: unknown) {
      showToast(`Could not add link: ${(err as Error).message}`, 'error')
    } finally {
      setSaving(false)
    }
  }

  const updateLink = async (link: SpaceLink, patch: Partial<SpaceLink>) => {
    try {
      await api.patch(
        `/api/spaces/${spaceId}/links/${link.id}`,
        patch,
      )
      await reload()
    } catch (err: unknown) {
      showToast(`Save failed: ${(err as Error).message}`, 'error')
    }
  }

  const deleteLink = async (link: SpaceLink) => {
    if (!confirm(`Remove "${link.label}"?`)) return
    try {
      await api.delete(`/api/spaces/${spaceId}/links/${link.id}`)
      await reload()
      showToast('Link removed.', 'info')
    } catch (err: unknown) {
      showToast(`Delete failed: ${(err as Error).message}`, 'error')
    }
  }

  const moveLink = async (index: number, delta: -1 | 1) => {
    const next = index + delta
    if (next < 0 || next >= links.length) return
    const a = links[index]
    const b = links[next]
    // Swap positions so the list order matches user intent.
    await Promise.all([
      api.patch(`/api/spaces/${spaceId}/links/${a.id}`, { position: b.position }),
      api.patch(`/api/spaces/${spaceId}/links/${b.id}`, { position: a.position }),
    ])
    await reload()
  }

  return (
    <section class="sh-space-links-tab">
      <h2>Quick links</h2>
      <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)' }}>
        Shortcuts shown to every member under the space header. Good for the
        household wiki, the shared grocery board, a shared calendar link, etc.
      </p>

      {loading && <p class="sh-muted">Loading…</p>}

      {!loading && links.length === 0 && (
        <p class="sh-muted">No links yet. Add one below.</p>
      )}

      {!loading && links.length > 0 && (
        <ul class="sh-space-links-editor" role="list">
          {links.map((link, i) => (
            <li key={link.id} class="sh-space-links-editor__row">
              <input
                class="sh-space-links-editor__label"
                value={link.label}
                aria-label="Link label"
                onBlur={(e) => {
                  const next = (e.target as HTMLInputElement).value.trim()
                  if (next && next !== link.label) {
                    void updateLink(link, { label: next })
                  }
                }}
              />
              <input
                class="sh-space-links-editor__url"
                value={link.url}
                aria-label="Link URL"
                onBlur={(e) => {
                  const next = (e.target as HTMLInputElement).value.trim()
                  if (next && next !== link.url) {
                    void updateLink(link, { url: next })
                  }
                }}
              />
              <div class="sh-space-links-editor__actions">
                <button type="button"
                        class="sh-icon-btn"
                        aria-label="Move up"
                        disabled={i === 0}
                        onClick={() => void moveLink(i, -1)}>↑</button>
                <button type="button"
                        class="sh-icon-btn"
                        aria-label="Move down"
                        disabled={i === links.length - 1}
                        onClick={() => void moveLink(i, 1)}>↓</button>
                <button type="button"
                        class="sh-icon-btn sh-icon-btn--danger"
                        aria-label={`Remove ${link.label}`}
                        onClick={() => void deleteLink(link)}>✕</button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <form class="sh-space-links-editor__create" onSubmit={createLink}>
        <h3>Add link</h3>
        <input type="text"
               value={draft.label}
               placeholder="Label (e.g. Family wiki)"
               maxLength={64}
               onInput={(e) =>
                 setDraft({ ...draft, label: (e.target as HTMLInputElement).value })
               } />
        <input type="url"
               value={draft.url}
               placeholder="https://…"
               maxLength={2048}
               onInput={(e) =>
                 setDraft({ ...draft, url: (e.target as HTMLInputElement).value })
               } />
        <Button type="submit" loading={saving}>Add link</Button>
      </form>
    </section>
  )
}
