/**
 * SpaceNotifPrefsMenu — per-member bell icon that controls the caller's
 * notification level for a single space.
 *
 * Levels (enforced server-side by NotificationService.on_space_post_created):
 *   - all       — every new post in this space pings the bell
 *   - mentions  — only posts that @mention the caller
 *   - muted     — nothing from this space
 *
 * Renders as a small inline menu — the current level is shown as an icon
 * on the button and a dropdown offers the three options. Any member can
 * set their own level; the backend route is 403 for non-members.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { api } from '@/api'
import { showToast } from '@/components/Toast'

type NotifLevel = 'all' | 'mentions' | 'muted'

interface Props {
  spaceId: string
}

const LEVEL_ICONS: Record<NotifLevel, string> = {
  all: '🔔',
  mentions: '@',
  muted: '🔕',
}

const LEVEL_LABELS: Record<NotifLevel, string> = {
  all: 'All posts',
  mentions: 'Only @mentions',
  muted: 'Muted',
}

export function SpaceNotifPrefsMenu({ spaceId }: Props) {
  const [level, setLevel] = useState<NotifLevel>('all')
  const [ready, setReady] = useState(false)
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const wrapRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    let stopped = false
    const load = async () => {
      try {
        const body = await api.get(
          `/api/spaces/${spaceId}/notif-prefs`,
        ) as { level: NotifLevel }
        if (!stopped) setLevel(body.level)
      } catch {
        // Non-member or network issue — leave default + hide menu later.
      } finally {
        if (!stopped) setReady(true)
      }
    }
    void load()
    return () => { stopped = true }
  }, [spaceId])

  useEffect(() => {
    if (!open) return
    const onClick = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('click', onClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('click', onClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  if (!ready) return null

  const choose = async (next: NotifLevel) => {
    if (saving) return
    if (next === level) { setOpen(false); return }
    setSaving(true)
    try {
      const body = await api.put(
        `/api/spaces/${spaceId}/notif-prefs`,
        { level: next },
      ) as { level: NotifLevel }
      setLevel(body.level)
      setOpen(false)
      showToast(
        next === 'muted'
          ? 'Muted — you won\'t see new post alerts from this space.'
          : next === 'mentions'
            ? 'You\'ll only be notified when someone @mentions you.'
            : 'You\'re getting all posts from this space.',
        'success',
      )
    } catch (err: unknown) {
      showToast(`Could not save: ${(err as Error).message}`, 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div class="sh-notif-prefs-menu" ref={wrapRef}>
      <button type="button"
              class="sh-notif-prefs-menu__trigger"
              aria-haspopup="menu"
              aria-expanded={open}
              title={`Notifications: ${LEVEL_LABELS[level]}`}
              onClick={() => setOpen(!open)}>
        <span aria-hidden="true">{LEVEL_ICONS[level]}</span>
        <span class="sh-visually-hidden">
          Notifications: {LEVEL_LABELS[level]}
        </span>
      </button>
      {open && (
        <div class="sh-notif-prefs-menu__panel" role="menu">
          {(['all', 'mentions', 'muted'] as NotifLevel[]).map(opt => (
            <button key={opt}
                    type="button"
                    role="menuitemradio"
                    aria-checked={level === opt}
                    class={level === opt
                      ? 'sh-notif-prefs-menu__item sh-notif-prefs-menu__item--active'
                      : 'sh-notif-prefs-menu__item'}
                    disabled={saving}
                    onClick={() => void choose(opt)}>
              <span aria-hidden="true">{LEVEL_ICONS[opt]}</span>
              <span>{LEVEL_LABELS[opt]}</span>
              {level === opt && <span aria-hidden="true">✓</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
