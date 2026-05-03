/**
 * SpaceNotifPrefsPanel — full-width radio panel for the per-member
 * notification preference, surfaced inside Space Settings.
 *
 * The dropdown variant (``SpaceNotifPrefsMenu``) lives in the space
 * subheader for quick access. Members who came looking for the
 * preference under "Settings" land here instead — same backend, same
 * three levels (``all`` / ``mentions`` / ``muted``), but the UI is
 * a clearly-labelled radio group with a one-line explainer per option.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { showToast } from '@/components/Toast'
import { Spinner } from '@/components/Spinner'

type NotifLevel = 'all' | 'mentions' | 'muted'

interface Props {
  spaceId: string
}

const LEVELS: ReadonlyArray<{
  level: NotifLevel
  icon: string
  title: string
  description: string
}> = [
  {
    level: 'all',
    icon: '🔔',
    title: 'All posts',
    description:
      'Bell-icon notification for every new post in this space.',
  },
  {
    level: 'mentions',
    icon: '@',
    title: 'Only @mentions',
    description:
      'Notify only when a post or comment @mentions you specifically.',
  },
  {
    level: 'muted',
    icon: '🔕',
    title: 'Muted',
    description:
      'No alerts at all. Posts still appear in the feed when you visit.',
  },
]

export function SpaceNotifPrefsPanel({ spaceId }: Props) {
  const [level, setLevel] = useState<NotifLevel>('all')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let stopped = false
    const load = async () => {
      try {
        const body = (await api.get(
          `/api/spaces/${spaceId}/notif-prefs`,
        )) as { level: NotifLevel }
        if (!stopped) setLevel(body.level)
      } catch {
        // Non-member or transient — leave the default; the backend
        // would 403 a non-member submitting anyway.
      } finally {
        if (!stopped) setLoading(false)
      }
    }
    void load()
    return () => {
      stopped = true
    }
  }, [spaceId])

  const choose = async (next: NotifLevel) => {
    if (saving || next === level) return
    setSaving(true)
    const previous = level
    setLevel(next)
    try {
      const body = (await api.put(
        `/api/spaces/${spaceId}/notif-prefs`,
        { level: next },
      )) as { level: NotifLevel }
      setLevel(body.level)
      showToast('Notification preference saved', 'success')
    } catch (err: unknown) {
      // Roll the optimistic flip back so the UI doesn't lie.
      setLevel(previous)
      showToast(
        `Could not save: ${(err as Error)?.message ?? err}`,
        'error',
      )
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <Spinner />

  return (
    <section class="sh-space-notif-prefs-panel">
      <h3>Notifications for this space</h3>
      <p class="sh-muted">
        How loud the bell should be when someone posts here. The
        preference is per-member — your housemates each pick their own.
      </p>
      <div class="sh-form sh-space-notif-prefs-list" role="radiogroup">
        {LEVELS.map(opt => (
          <label
            key={opt.level}
            class={
              level === opt.level
                ? 'sh-mode-option sh-mode-option--selected'
                : 'sh-mode-option'
            }
          >
            <input
              type="radio"
              name={`notif-prefs-${spaceId}`}
              value={opt.level}
              checked={level === opt.level}
              disabled={saving}
              onChange={() => void choose(opt.level)}
            />
            <span class="sh-mode-option__body">
              <span class="sh-mode-option__title">
                <span aria-hidden="true">{opt.icon}</span> {opt.title}
              </span>
              <span class="sh-muted">{opt.description}</span>
            </span>
          </label>
        ))}
      </div>
    </section>
  )
}
