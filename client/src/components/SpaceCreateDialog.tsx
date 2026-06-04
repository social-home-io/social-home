/**
 * SpaceCreateDialog — space creation flow (§23.50).
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { loadSpaces } from '@/store/spaces'
import { Modal } from './Modal'
import { Button } from './Button'
import { EmojiField } from './EmojiField'
import { RadioCardGroup } from './RadioCardGroup'
import { VISIBILITY_OPTIONS, joinOptionsForVisibility } from './spaceModeOptions'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'

const open = signal(false)
const name = signal('')
const description = signal('')
const emoji = signal('')
const spaceType = signal('private')
const joinMode = signal('invite_only')
// Public spaces are pinned on the public map, so they need a location.
// Held as strings for the inputs; parsed + (server-side) truncated to 4dp.
const lat = signal('')
const lon = signal('')
const locating = signal(false)
const submitting = signal(false)

export function openSpaceCreate() {
  open.value = true
  name.value = ''
  description.value = ''
  emoji.value = ''
  spaceType.value = 'private'
  joinMode.value = 'invite_only'
  lat.value = ''
  lon.value = ''
  locating.value = false
}

const isPublic = () => spaceType.value === 'public'
// Public needs both coordinates before the backend will accept it.
const locationReady = () =>
  !isPublic() || (lat.value.trim() !== '' && lon.value.trim() !== '')

function useMyLocation() {
  if (!navigator.geolocation) {
    showToast('Location is not available in this browser.', 'error')
    return
  }
  locating.value = true
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      // Truncate to 4dp here too (the backend also truncates) — never
      // store/transmit raw device precision (§GPS).
      lat.value = String(Math.round(pos.coords.latitude * 1e4) / 1e4)
      lon.value = String(Math.round(pos.coords.longitude * 1e4) / 1e4)
      locating.value = false
    },
    () => {
      showToast('Couldn\'t get your location — enter it manually.', 'info')
      locating.value = false
    },
  )
}

export function SpaceCreateDialog() {
  const handleSubmit = async () => {
    if (!name.value.trim() || !locationReady() || submitting.value) return
    submitting.value = true
    try {
      await api.post('/api/spaces', {
        name: name.value,
        description: description.value || undefined,
        emoji: emoji.value || undefined,
        space_type: spaceType.value,
        join_mode: joinMode.value,
        ...(isPublic()
          ? { lat: Number(lat.value), lon: Number(lon.value) }
          : {}),
      })
      // Refresh the cached spaces list so the new row appears on the
      // list page without a hard reload.
      await loadSpaces()
      showToast('Space created', 'success')
      open.value = false
    } catch (e: any) {
      showToast(e.message || 'Failed to create space', 'error')
    } finally {
      submitting.value = false
    }
  }

  return (
    <Modal open={open.value} onClose={() => open.value = false} title="Create a space">
      <div class="sh-form">
        <label>
          Name *
          <input value={name.value} onInput={(e) => name.value = (e.target as HTMLInputElement).value}
            placeholder="e.g. Family, Makers Club" />
        </label>
        <label>
          Description
          <textarea value={description.value}
            onInput={(e) => description.value = (e.target as HTMLTextAreaElement).value}
            placeholder="What's this space about?" rows={2} />
        </label>
        <EmojiField value={emoji} openKey="space-create-icon" />
        <RadioCardGroup
          legend="Visibility"
          name="space-create-visibility"
          value={spaceType.value}
          options={VISIBILITY_OPTIONS}
          onChange={(v) => {
            spaceType.value = v
            // A private space is invite-only by definition — there's no
            // join-mode choice to make, so keep it consistent.
            if (v === 'private') joinMode.value = 'invite_only'
          }}
        />
        <RadioCardGroup
          legend="How people join"
          name="space-create-join-mode"
          value={joinMode.value}
          options={joinOptionsForVisibility(spaceType.value)}
          onChange={(v) => joinMode.value = v}
        />
        {isPublic() && (
          <fieldset class="sh-form-fieldset sh-space-create-location">
            <legend>📍 Map location</legend>
            <p class="sh-muted" style={{ marginTop: 0 }}>
              Public spaces are pinned on the map so people nearby can find
              them. Coordinates are rounded to ~11 m.
            </p>
            <Button
              variant="secondary"
              onClick={useMyLocation}
              loading={locating.value}
            >
              📍 Use my location
            </Button>
            <div class="sh-row" style={{ gap: 'var(--sh-space-sm)' }}>
              <label>
                Latitude
                <input
                  type="number" inputMode="decimal" step="0.0001"
                  min={-90} max={90}
                  placeholder="52.5200"
                  value={lat.value}
                  onInput={(e) => lat.value = (e.target as HTMLInputElement).value}
                />
              </label>
              <label>
                Longitude
                <input
                  type="number" inputMode="decimal" step="0.0001"
                  min={-180} max={180}
                  placeholder="13.4050"
                  value={lon.value}
                  onInput={(e) => lon.value = (e.target as HTMLInputElement).value}
                />
              </label>
            </div>
          </fieldset>
        )}
        <div class="sh-form-actions">
          <Button variant="secondary" onClick={() => open.value = false}>{t('common.cancel')}</Button>
          <Button onClick={handleSubmit} loading={submitting.value}
            disabled={!name.value.trim() || !locationReady()}>
            Create
          </Button>
        </div>
      </div>
    </Modal>
  )
}
