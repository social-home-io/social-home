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
import { VISIBILITY_OPTIONS, JOIN_MODE_OPTIONS } from './spaceModeOptions'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'

const open = signal(false)
const name = signal('')
const description = signal('')
const emoji = signal('')
const spaceType = signal('private')
const joinMode = signal('invite_only')
const submitting = signal(false)

export function openSpaceCreate() {
  open.value = true
  name.value = ''
  description.value = ''
  emoji.value = ''
  spaceType.value = 'private'
  joinMode.value = 'invite_only'
}

export function SpaceCreateDialog() {
  const handleSubmit = async () => {
    if (!name.value.trim() || submitting.value) return
    submitting.value = true
    try {
      await api.post('/api/spaces', {
        name: name.value,
        description: description.value || undefined,
        emoji: emoji.value || undefined,
        space_type: spaceType.value,
        join_mode: joinMode.value,
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
        {spaceType.value !== 'private' && (
          <RadioCardGroup
            legend="How people join"
            name="space-create-join-mode"
            value={joinMode.value}
            options={JOIN_MODE_OPTIONS}
            onChange={(v) => joinMode.value = v}
          />
        )}
        <div class="sh-form-actions">
          <Button variant="secondary" onClick={() => open.value = false}>{t('common.cancel')}</Button>
          <Button onClick={handleSubmit} loading={submitting.value}
            disabled={!name.value.trim()}>
            Create
          </Button>
        </div>
      </div>
    </Modal>
  )
}
