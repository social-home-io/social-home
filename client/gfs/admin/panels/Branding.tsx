import { useCallback, useEffect, useState } from 'preact/hooks'
import { api } from '../api'

interface Branding {
  server_name: string
  landing_markdown: string
  header_image_file: string
}

export function BrandingPanel({ onSaved }: { onSaved?: () => void }) {
  const [branding, setBranding] = useState<Branding | null>(null)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    try {
      const data = await api<Branding>('GET', '/admin/api/branding')
      setBranding(data)
      setErr(null)
    } catch (e) {
      setErr((e as Error).message)
    }
  }, [])

  useEffect(() => { void reload() }, [reload])

  const save = async () => {
    if (!branding) return
    setSaving(true)
    try {
      await api('PATCH', '/admin/api/branding', branding)
      await reload()
      onSaved?.()
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  if (err) return <p class="error">{err}</p>
  if (!branding) return <p class="muted">Loading…</p>
  const update = <K extends keyof Branding>(key: K, value: Branding[K]) =>
    setBranding({ ...branding, [key]: value })

  return (
    <>
      <h2>Branding</h2>
      <div class="field">
        <label>
          Server name
          <input
            value={branding.server_name}
            onInput={(e) => update('server_name', (e.currentTarget as HTMLInputElement).value)}
          />
        </label>
      </div>
      <div class="field">
        <label>
          Landing markdown
          <textarea
            value={branding.landing_markdown}
            onInput={(e) => update('landing_markdown', (e.currentTarget as HTMLTextAreaElement).value)}
          />
        </label>
      </div>
      <div class="field">
        <label>
          Header image filename
          <input
            value={branding.header_image_file}
            onInput={(e) => update('header_image_file', (e.currentTarget as HTMLInputElement).value)}
          />
        </label>
      </div>
      <button class="primary" onClick={() => void save()} disabled={saving}>
        {saving ? 'Saving…' : 'Save'}
      </button>
    </>
  )
}
