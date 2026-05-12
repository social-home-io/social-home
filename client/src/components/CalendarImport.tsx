/**
 * CalendarImport — three ways to add events to a calendar (§5.2).
 *
 *   1. Upload a .ics / .vcs file → POST /api/calendars/{id}/import_ics
 *   2. From a photo (AI)         → POST /api/calendars/{id}/import_image
 *   3. From a description (AI)   → POST /api/calendars/{id}/import_prompt
 *
 * The server parses vCalendar in all three paths; the AI paths work only
 * when the platform adapter has an ai_task backend configured.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { token } from '@/store/auth'
import { hasCapability } from '@/store/instance'
import { Button } from './Button'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'

type Mode = null | 'menu' | 'image' | 'prompt'

const mode = signal<Mode>(null)
const busy = signal(false)
const caption = signal('')
const prompt = signal('')

function close(): void {
  mode.value = null
  caption.value = ''
  prompt.value = ''
}

export function CalendarImport({ calendarId }: { calendarId: string }) {
  const handleIcsPick = () => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = '.ics,.ical,.vcs,text/calendar'
    input.onchange = async () => {
      const file = input.files?.[0]
      if (!file) return
      busy.value = true
      try {
        const res = await fetch(`api/calendars/${calendarId}/import_ics`, {
          method: 'POST',
          headers: {
            'Content-Type': 'text/calendar',
            ...(token.value ? { Authorization: `Bearer ${token.value}` } : {}),
          },
          body: await file.arrayBuffer(),
        })
        const data = await res.json()
        if (!res.ok) throw new Error(data?.error?.detail || `Import failed (${res.status})`)
        showToast(`Imported ${data.events.length} event(s)`, 'success')
        close()
      } catch (e: any) { showToast(e.message || 'Import failed', 'error') }
      finally { busy.value = false }
    }
    input.click()
  }

  const handleImagePick = () => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = 'image/*'
    input.onchange = async () => {
      const file = input.files?.[0]
      if (!file) return
      busy.value = true
      try {
        const dataUrl = await readAsDataUrl(file)
        const data = await api.post<{ events: unknown[] }>(
          `/api/calendars/${calendarId}/import_image`,
          { image_data_url: dataUrl, caption: caption.value || undefined },
        )
        showToast(`Imported ${data.events.length} event(s)`, 'success')
        close()
      } catch (e: any) { showToast(e.message || 'Import failed', 'error') }
      finally { busy.value = false }
    }
    input.click()
  }

  const handlePromptSubmit = async () => {
    const text = prompt.value.trim()
    if (!text) return
    busy.value = true
    try {
      const data = await api.post<{ events: unknown[] }>(
        `/api/calendars/${calendarId}/import_prompt`,
        { prompt: text },
      )
      showToast(`Imported ${data.events.length} event(s)`, 'success')
      close()
    } catch (e: any) { showToast(e.message || 'Import failed', 'error') }
    finally { busy.value = false }
  }

  return (
    <>
      <Button variant="secondary" onClick={() => { mode.value = 'menu' }}>
        Add events
      </Button>
      {mode.value === 'menu' && (
        <div class="modal" onClick={close}>
          <div class="modal-body" onClick={(e) => e.stopPropagation()}>
            <h3>Add events</h3>
            <Button onClick={handleIcsPick} loading={busy.value}>
              Upload .ics / .vcs file
            </Button>
            {/* AI-backed import only renders when the adapter exposes an
             *  ``ai`` capability. On standalone the endpoints 501 and
             *  the buttons opened a dead path — quieter to hide them.
             *  HA / HAOS surface the AI options once an ai_task backend
             *  is configured. */}
            {hasCapability('ai') && (
              <>
                <Button onClick={() => { mode.value = 'image' }} loading={busy.value}>
                  From a photo
                </Button>
                <Button onClick={() => { mode.value = 'prompt' }} loading={busy.value}>
                  From a description
                </Button>
              </>
            )}
            <Button variant="secondary" onClick={close}>{t('common.cancel')}</Button>
          </div>
        </div>
      )}
      {mode.value === 'image' && (
        <div class="modal" onClick={close}>
          <div class="modal-body" onClick={(e) => e.stopPropagation()}>
            <h3>Import from photo</h3>
            <label>
              <span class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)' }}>
                Note to the AI (optional)
              </span>
              <input
                type="text"
                placeholder="Optional note to the AI…"
                value={caption.value}
                onInput={(e) => { caption.value = (e.target as HTMLInputElement).value }}
              />
            </label>
            <Button onClick={handleImagePick} loading={busy.value}>Choose image</Button>
            <Button variant="secondary" onClick={close}>{t('common.cancel')}</Button>
          </div>
        </div>
      )}
      {mode.value === 'prompt' && (
        <div class="modal" onClick={close}>
          <div class="modal-body" onClick={(e) => e.stopPropagation()}>
            <h3>Describe the event</h3>
            <textarea
              rows={4}
              placeholder="e.g. dentist next Tuesday 10am"
              value={prompt.value}
              onInput={(e) => { prompt.value = (e.target as HTMLTextAreaElement).value }}
            />
            <Button onClick={handlePromptSubmit} loading={busy.value}>Create events</Button>
            <Button variant="secondary" onClick={close}>{t('common.cancel')}</Button>
          </div>
        </div>
      )}
    </>
  )
}

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(String(r.result))
    r.onerror = () => reject(r.error)
    r.readAsDataURL(file)
  })
}
