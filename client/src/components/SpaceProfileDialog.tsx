/**
 * SpaceProfileDialog — lets the caller set a different display name +
 * avatar for a specific space (§4.1.6 / §23).
 *
 * - Display-name change → PATCH /api/spaces/{id}/members/me
 * - Picture upload      → POST /api/spaces/{id}/members/me/picture
 * - "Reset to household" clears both overrides.
 */
import { signal } from '@preact/signals'
import { useEffect, useRef, useState } from 'preact/hooks'
import { api } from '@/api'
import { Avatar } from './Avatar'
import { Button } from './Button'
import { Modal } from './Modal'
import { showToast } from './Toast'
import { currentUser } from '@/store/auth'
import {
  loadSpaceMembers,
  spaceMembers,
  invalidateSpaceMembers,
} from '@/store/spaceMembers'
import { confirmDialog } from '@/components/confirm'

const open = signal(false)
const activeSpaceId = signal<string | null>(null)

export function openSpaceProfileDialog(spaceId: string): void {
  activeSpaceId.value = spaceId
  open.value = true
}

export function SpaceProfileDialog() {
  const spaceId = activeSpaceId.value
  const [displayName, setDisplayName] = useState('')
  const [saving, setSaving] = useState(false)
  const [localPictureUrl, setLocalPictureUrl] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    if (!open.value || !spaceId) return
    void loadSpaceMembers(spaceId)
    const me = currentUser.value?.user_id
    if (!me) return
    const cached = spaceMembers.value[spaceId]?.get(me)
    setDisplayName(cached?.space_display_name ?? '')
    setLocalPictureUrl(cached?.picture_url ?? null)
  }, [open.value, spaceId])

  if (!spaceId) return null

  const close = () => {
    open.value = false
    activeSpaceId.value = null
  }

  const saveName = async () => {
    if (!spaceId) return
    setSaving(true)
    try {
      await api.patch(
        `/api/spaces/${spaceId}/members/me`,
        { space_display_name: displayName.trim() || null },
      )
      invalidateSpaceMembers(spaceId)
      await loadSpaceMembers(spaceId)
      showToast('Space name updated', 'success')
    } catch (err: unknown) {
      showToast(
        `Save failed: ${(err as Error).message ?? err}`, 'error',
      )
    } finally {
      setSaving(false)
    }
  }

  const uploadPicture = async (e: Event) => {
    const input = e.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file || !spaceId) return
    setSaving(true)
    try {
      const fd = new FormData()
      fd.append('file', file)
      const updated = await api.upload(
        `/api/spaces/${spaceId}/members/me/picture`, fd,
      ) as { picture_url: string }
      setLocalPictureUrl(updated.picture_url)
      invalidateSpaceMembers(spaceId)
      await loadSpaceMembers(spaceId)
      showToast('Space avatar updated', 'success')
    } catch (err: unknown) {
      showToast(
        `Upload failed: ${(err as Error).message ?? err}`, 'error',
      )
    } finally {
      setSaving(false)
      input.value = ''
    }
  }

  const clearPicture = async () => {
    if (!spaceId) return
    if (!await confirmDialog('Reset your space avatar to your household picture?', { destructive: true })) return
    setSaving(true)
    try {
      await api.delete(`/api/spaces/${spaceId}/members/me/picture`)
      setLocalPictureUrl(null)
      invalidateSpaceMembers(spaceId)
      await loadSpaceMembers(spaceId)
      showToast('Space avatar cleared', 'info')
    } catch (err: unknown) {
      showToast(
        `Reset failed: ${(err as Error).message ?? err}`, 'error',
      )
    } finally {
      setSaving(false)
    }
  }

  const inheritedName = currentUser.value?.display_name ?? ''
  const householdPictureUrl = currentUser.value?.picture_url ?? null
  const usingInheritedName = !displayName.trim()
  const usingInheritedPicture = !localPictureUrl

  return (
    <Modal open={open.value} onClose={close}
           title="Your profile in this space">
      <div class="sh-form sh-space-profile-dialog">
        <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)' }}>
          Set a different display name or avatar for this space. Leave
          fields blank to inherit your household defaults.
        </p>

        <div class="sh-profile-card">
          <label class="sh-profile-avatar-slot"
                 title="Click to upload a new space avatar">
            <Avatar
              name={displayName || inheritedName || '?'}
              src={localPictureUrl || householdPictureUrl}
              size={96} />
            <span class="sh-profile-avatar-hint" aria-hidden="true">
              📷 Upload
            </span>
            <input ref={fileRef} type="file" accept="image/*"
                   onChange={uploadPicture} class="sr-only" />
          </label>
          <div class="sh-profile-card-meta">
            <div class="sh-profile-identity">
              <strong class="sh-profile-name">
                {displayName || inheritedName || '—'}
              </strong>
              <span class="sh-muted">
                In this space
              </span>
            </div>
            <span class={`sh-profile-source ${usingInheritedPicture ? 'sh-profile-source--manual' : 'sh-profile-source--ha'}`}>
              {usingInheritedPicture
                ? '⬇ Using household picture'
                : '✨ Custom for this space'}
            </span>
            {localPictureUrl && (
              <Button variant="secondary" onClick={clearPicture}
                      loading={saving}>
                Reset to household picture
              </Button>
            )}
          </div>
        </div>

        <label>
          Display name in this space
          <input type="text" value={displayName} maxLength={64}
                 placeholder={inheritedName}
                 onInput={(e) =>
                   setDisplayName((e.target as HTMLInputElement).value)} />
          <span class="sh-char-count">
            {usingInheritedName
              ? `Inheriting household name: ${inheritedName || '(unset)'}`
              : `Override for this space`}
          </span>
        </label>

        <div class="sh-form-actions">
          <Button variant="secondary" onClick={close}>Cancel</Button>
          <Button onClick={saveName} loading={saving}>Save</Button>
        </div>
      </div>
    </Modal>
  )
}
