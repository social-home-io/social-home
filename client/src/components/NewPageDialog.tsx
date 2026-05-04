/**
 * NewPageDialog — modal replacement for the old window.prompt flow.
 *
 * Composes on top of ``Modal`` so it inherits the household focus-trap
 * + Escape + focus-restore behaviour all other dialogs share.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { Modal } from './Modal'
import { Button } from './Button'
import { t } from '@/i18n/i18n'

interface Props {
  open: boolean
  onCreate: (title: string) => void | Promise<void>
  onCancel: () => void
}

export function NewPageDialog({ open, onCreate, onCancel }: Props) {
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const ref = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    if (open) {
      setTitle('')
      setBusy(false)
      // ``Modal`` focuses the first focusable on open. The title input
      // is the first interactive element below, so this nudge keeps
      // the caret in the field after the StrictMode double-mount + the
      // input's own value/placeholder hydration completes.
      setTimeout(() => ref.current?.focus(), 10)
    }
  }, [open])

  const submit = async (e: Event) => {
    e.preventDefault()
    const t = title.trim()
    if (!t || busy) return
    setBusy(true)
    try { await onCreate(t) }
    finally { setBusy(false) }
  }

  return (
    <Modal open={open} onClose={onCancel} title="New page">
      <form class="sh-form" onSubmit={submit}>
        <label>
          Title
          <input
            ref={ref}
            value={title}
            placeholder="e.g. Trip plan — Italy 2026"
            onInput={(e) => setTitle((e.target as HTMLInputElement).value)}
            required
          />
        </label>
        <div class="sh-form-actions">
          <Button variant="secondary" type="button" onClick={onCancel}>{t('common.cancel')}</Button>
          <Button type="submit" loading={busy} disabled={!title.trim()}>
            Create
          </Button>
        </div>
      </form>
    </Modal>
  )
}
