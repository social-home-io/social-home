/**
 * FollowedSpacesPicker — modal that lets the caller tick which of
 * their member spaces surface in the :mod:`DashboardPage` "Spaces
 * you follow" widget (§23).
 *
 * Persists the selection to the caller's user-preferences as
 * ``followed_space_ids: string[]`` — no schema change, no
 * membership mutation.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { Button } from './Button'
import { Modal } from './Modal'
import { showToast } from './Toast'
import { getPreferences, setPreference } from '@/utils/preferences'
import { t } from '@/i18n/i18n'

interface SpaceRow {
  id: string
  name: string
  emoji: string | null
}

interface Props {
  open: boolean
  onClose: () => void
  onChanged?: (followedIds: string[]) => void
}

const MAX_FOLLOWED = 10

export function FollowedSpacesPicker({ open, onClose, onChanged }: Props) {
  const [spaces, setSpaces] = useState<SpaceRow[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!open) return
    setLoading(true)
    void (async () => {
      try {
        const rows = await api.get('/api/spaces') as SpaceRow[]
        setSpaces(rows)
        const prefs = getPreferences()
        setSelected(new Set(prefs.followed_space_ids ?? []))
      } catch (err: unknown) {
        showToast(
          `Could not load spaces: ${(err as Error).message ?? err}`,
          'error',
        )
      } finally {
        setLoading(false)
      }
    })()
  }, [open])

  if (!open) return null

  const toggle = (id: string) => {
    const next = new Set(selected)
    if (next.has(id)) {
      next.delete(id)
    } else {
      if (next.size >= MAX_FOLLOWED) {
        showToast(
          `You can follow at most ${MAX_FOLLOWED} spaces.`,
          'info',
        )
        return
      }
      next.add(id)
    }
    setSelected(next)
  }

  const save = async () => {
    setSaving(true)
    try {
      const ids = spaces
        .filter(s => selected.has(s.id))
        .map(s => s.id)
      await setPreference('followed_space_ids', ids)
      showToast(
        ids.length
          ? `Following ${ids.length} ${ids.length === 1 ? 'space' : 'spaces'}`
          : 'Cleared followed spaces',
        'success',
      )
      onChanged?.(ids)
      onClose()
    } catch (err: unknown) {
      showToast(
        `Save failed: ${(err as Error).message ?? err}`, 'error',
      )
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal open={open} onClose={onClose}
           title="Follow spaces on your dashboard">
      <div class="sh-form sh-followed-picker">
        <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)' }}>
          Pick up to {MAX_FOLLOWED} spaces. You'll see their newest
          posts in the "Spaces you follow" widget on My Corner.
        </p>

        <div class="sh-followed-picker-count">
          <strong>{selected.size}</strong>
          <span class="sh-muted">
            {' '}of {spaces.length} {spaces.length === 1 ? 'space' : 'spaces'} followed
          </span>
        </div>

        {loading ? (
          <p class="sh-muted">Loading your spaces…</p>
        ) : spaces.length === 0 ? (
          <p class="sh-muted">You're not a member of any spaces yet.</p>
        ) : (
          <ul class="sh-followed-picker-list">
            {spaces.map(s => (
              <li key={s.id}>
                <label class={`sh-followed-picker-row ${
                  selected.has(s.id) ? 'sh-followed-picker-row--selected' : ''
                }`}>
                  <input type="checkbox"
                         checked={selected.has(s.id)}
                         onChange={() => toggle(s.id)} />
                  <span class="sh-followed-picker-emoji" aria-hidden="true">
                    {s.emoji || '🪐'}
                  </span>
                  <span class="sh-followed-picker-name">{s.name}</span>
                </label>
              </li>
            ))}
          </ul>
        )}

        <div class="sh-form-actions">
          <Button variant="secondary" onClick={onClose}>{t('common.cancel')}</Button>
          <Button onClick={save} loading={saving}>{t('common.save')}</Button>
        </div>
      </div>
    </Modal>
  )
}
