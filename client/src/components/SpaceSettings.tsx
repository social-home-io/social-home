/**
 * SpaceSettings — space admin settings panel (§23.91).
 * Includes a Federation section for GFS publish/unpublish.
 */
import { useEffect, useState } from 'preact/hooks'
import { signal, useSignal } from '@preact/signals'
import { api } from '@/api'
import { Button } from './Button'
import { ConfirmDialog } from './ConfirmDialog'
import { EmojiField } from './EmojiField'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'
import type { Space, GfsConnection, GfsSpacePublication } from '@/types'

// Post types an admin can enable/disable for the space feed (§23.49) —
// the same set the space composer offers, in composer order, so each
// toggle maps 1:1 to a button members see. Types the backend tracks but
// that aren't composed via the picker (transcript / event /
// highlight_share) are intentionally absent here and preserved untouched
// on save (see ``save``), so toggling these never disables them.
const SPACE_POST_TYPES: [string, string][] = [
  ['text', '🔤 Text'],
  ['image', '📷 Image'],
  ['video', '🎬 Video'],
  ['file', '📄 File'],
  ['poll', '📊 Poll'],
  ['schedule', '📅 Schedule'],
  ['location', '📍 Location'],
  ['highlight_share', '⭕ Highlight share'],
]
const SPACE_POST_TYPE_KEYS = SPACE_POST_TYPES.map(([k]) => k)

const showDissolve = signal(false)
const gfsServers = signal<GfsConnection[]>([])
const publications = signal<GfsSpacePublication[]>([])
const federationLoading = signal(false)

async function loadFederationData(spaceId: string) {
  federationLoading.value = true
  try {
    const [servers, pubs] = await Promise.all([
      api.get<GfsConnection[]>('/api/gfs/connections'),
      api.get<GfsSpacePublication[]>(`/api/spaces/${spaceId}/publications`),
    ])
    gfsServers.value = servers
    publications.value = pubs
  } catch {
    gfsServers.value = []
    publications.value = []
  }
  federationLoading.value = false
}

function isPublished(gfsId: string): boolean {
  return publications.value.some(p => p.gfs_connection_id === gfsId)
}

/**
 * Build the GFS-side public URL for this space.
 *
 * The GFS exposes a server-rendered ``/spaces/{space_id}`` page (see
 * ``socialhome/global_server/public.py``). ``inbox_url`` is stored on
 * the connection as the GFS root (``https://gfs.example.com``) — the
 * routes ``/gfs/...`` are concatenated onto it for federation calls,
 * so stripping a trailing slash is enough to land on the public root
 * for browser links.
 */
function publicSpaceUrl(inboxUrl: string, spaceId: string): string {
  return `${inboxUrl.replace(/\/+$/, '')}/spaces/${spaceId}`
}

async function copyToClipboard(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    showToast('Public link copied', 'success')
  } catch {
    // Older browsers / locked-down WebViews fall through to a manual
    // selection prompt rather than silently failing.
    showToast('Couldn\'t copy — long-press the link to copy manually', 'info')
  }
}

async function togglePublish(spaceId: string, gfsId: string) {
  try {
    if (isPublished(gfsId)) {
      await api.delete(`/api/spaces/${spaceId}/publish/${gfsId}`)
      publications.value = publications.value.filter(p => p.gfs_connection_id !== gfsId)
      showToast(t('space.unpublish_from_gfs'), 'success')
    } else {
      const pub = await api.post<GfsSpacePublication>(`/api/spaces/${spaceId}/publish/${gfsId}`)
      publications.value = [...publications.value, pub]
      showToast(t('space.publish_to_gfs'), 'success')
    }
  } catch (e: any) {
    showToast(e.message || 'Failed', 'error')
  }
}

export function SpaceSettings({ space, onUpdate }: { space: Space; onUpdate: () => void }) {
  // ``useSignal`` (not ``signal()``) so the underlying signal instance
  // is stable across renders. Plain ``signal(initial)`` inside a
  // component body recreates a fresh signal on every render, which
  // silently drops typed values when sibling state changes trigger a
  // re-render — caught the hard way during the retention-days work.
  const name = useSignal(space.name)
  const description = useSignal(space.description || '')
  const emoji = useSignal(space.emoji || '')
  const joinMode = useSignal(space.join_mode)
  const locationEnabled = useSignal(Boolean(space.features?.location))
  const locationMode = useSignal<'gps' | 'zone_only'>(
    space.features?.location_mode ?? 'gps',
  )
  // Per-space feature visibility (§23.91). Admins toggle these to hide
  // tabs that aren't used in the space; existing data (pages, events,
  // tasks, stickies, gallery albums) stays in storage and reappears
  // when the flag flips back. Defaults mirror the SpaceFeatures
  // dataclass — every tab on by default. Pre-0008/0009 spaces with
  // a column at 0 are backfilled to 1 by the migrations.
  const featurePages = useSignal(space.features?.pages ?? true)
  const featureCalendar = useSignal(space.features?.calendar ?? true)
  const featureTodo = useSignal(space.features?.todo ?? true)
  const featureStickies = useSignal(space.features?.stickies ?? true)
  const featureGallery = useSignal(space.features?.gallery ?? true)
  const featureBazaar = useSignal(space.features?.bazaar ?? true)
  // Subscriber-engagement opt-ins (§23.49) — admins flip these when
  // they want followers to be able to react / comment without being
  // promoted to full members.  Posts always remain member-only.
  const allowSubscriberComment = useSignal(
    Boolean(space.features?.allow_subscriber_comment),
  )
  const allowSubscriberReact = useSignal(
    Boolean(space.features?.allow_subscriber_react),
  )
  // Per-space post-type allow-list (§23.49). A missing list means a
  // freshly-stubbed space the host config hasn't reached yet — treat
  // that as all-allowed so the checkboxes don't render everything off.
  const allowedPostTypes = space.features?.allowed_post_types
  const postTypeEnabled = useSignal<Record<string, boolean>>(
    Object.fromEntries(
      SPACE_POST_TYPE_KEYS.map((k) => [
        k,
        !allowedPostTypes || allowedPostTypes.includes(k),
      ]),
    ),
  )
  // Retention is "delete posts older than N days". ``null`` means
  // "keep forever" — that's the legacy default and what fresh spaces
  // ship with. The text input is empty in that case; entering 0 or
  // clearing the field flips it back to "forever". The backend
  // service normalises any non-positive value to ``null``.
  const [retentionDays, setRetentionDays] = useState<string>(
    space.retention_days != null ? String(space.retention_days) : '',
  )

  useEffect(() => {
    loadFederationData(space.id)
  }, [space.id])

  const save = async () => {
    const previousMode = space.features?.location_mode ?? 'gps'
    const modeChanged = locationEnabled.value
      && locationMode.value !== previousMode
    // Empty / zero / negative → 0 sentinel which the backend normalises
    // back to ``retention_days = null`` ("no limit"). A positive integer
    // is sent verbatim.
    const parsedRetention = parseInt(retentionDays, 10)
    const retentionPayload: number | undefined =
      retentionDays.trim() === ''
        ? 0
        : Number.isFinite(parsedRetention)
          ? parsedRetention
          : undefined
    // Rebuild allowed_post_types from the checkboxes, but PRESERVE any
    // type the UI doesn't manage (transcript / event / highlight_share)
    // exactly as the space already had it — otherwise saving settings
    // would silently strip them.
    const existingAllowed =
      space.features?.allowed_post_types
      ?? SPACE_POST_TYPE_KEYS.slice()
    const preserved = existingAllowed.filter(
      (t) => !SPACE_POST_TYPE_KEYS.includes(t),
    )
    const chosen = SPACE_POST_TYPE_KEYS.filter((k) => postTypeEnabled.value[k])
    if (chosen.length === 0) {
      showToast('Enable at least one post type for the feed', 'error')
      return
    }
    const allowedPostTypesPayload = [...preserved, ...chosen].sort()
    try {
      await api.patch(`/api/spaces/${space.id}`, {
        name: name.value,
        description: description.value || undefined,
        emoji: emoji.value || undefined,
        join_mode: joinMode.value,
        ...(retentionPayload !== undefined
          ? { retention_days: retentionPayload }
          : {}),
        features: {
          ...(space.features as object),
          pages: featurePages.value,
          calendar: featureCalendar.value,
          todo: featureTodo.value,
          stickies: featureStickies.value,
          gallery: featureGallery.value,
          bazaar: featureBazaar.value,
          location: locationEnabled.value,
          location_mode: locationMode.value,
          allow_subscriber_comment: allowSubscriberComment.value,
          allow_subscriber_react: allowSubscriberReact.value,
          allowed_post_types: allowedPostTypesPayload,
        },
      })
      if (modeChanged) {
        showToast(
          locationMode.value === 'zone_only'
            ? 'Zone-only mode on. Members will see only zone labels within seconds.'
            : 'Live GPS mode on. Members will see GPS pins within seconds.',
          'success',
        )
      } else {
        showToast('Space updated', 'success')
      }
      onUpdate()
    } catch (e: any) {
      showToast(e.message || 'Failed to update', 'error')
    }
  }

  const dissolve = async () => {
    try {
      await api.delete(`/api/spaces/${space.id}`)
      showToast('Space dissolved', 'info')
      location.href = '/spaces'
    } catch (e: any) {
      showToast(e.message || 'Failed to dissolve', 'error')
    }
  }

  const setArchived = async (archived: boolean) => {
    try {
      if (archived) await api.post(`/api/spaces/${space.id}/archive`)
      else await api.delete(`/api/spaces/${space.id}/archive`)
      showToast(
        archived ? 'Space archived — now read-only' : 'Space unarchived',
        'success',
      )
      onUpdate()
    } catch (e: any) {
      showToast(e.message || 'Failed to update archive state', 'error')
    }
  }

  return (
    <div class="sh-space-settings">
      <h3>Space Settings</h3>
      <div class="sh-form">
        <label>Name <input value={name.value} onInput={(e) => name.value = (e.target as HTMLInputElement).value} /></label>
        <label>Description <textarea value={description.value} onInput={(e) => description.value = (e.target as HTMLTextAreaElement).value} rows={2} /></label>
        <EmojiField value={emoji} openKey="space-settings-icon" />
        <label>Join mode
          <select value={joinMode.value} onChange={(e) => joinMode.value = (e.target as HTMLSelectElement).value as any}>
            <option value="invite_only">Invite only</option>
            <option value="open">Open</option>
            <option value="link">Link</option>
            <option value="request">Request</option>
          </select>
        </label>
        <fieldset class="sh-form-fieldset">
          <legend>🗓 Retention</legend>
          <label>
            Auto-delete posts older than
            <input
              type="number"
              min={0}
              max={3650}
              inputMode="numeric"
              value={retentionDays}
              placeholder="Forever"
              onInput={(e) => {
                setRetentionDays((e.target as HTMLInputElement).value)
              }}
            />
            <span class="sh-muted"> days (leave empty or 0 to keep forever)</span>
          </label>
          <p class="sh-muted">
            Applies to feed posts and comments in this space. Calendar
            events and pages are exempt by default.
          </p>
        </fieldset>
        <fieldset class="sh-form-fieldset" data-testid="space-features">
          <legend>🧩 Features</legend>
          <p class="sh-muted" style={{ marginTop: 0 }}>
            Hide tabs that aren't used in this space. Existing pages,
            events, tasks, stickies, or gallery albums stay in storage
            and reappear when you turn the toggle back on.
          </p>
          <label class="sh-toggle-row">
            <input
              type="checkbox"
              checked={featurePages.value}
              onChange={(e) => {
                featurePages.value = (e.target as HTMLInputElement).checked
              }}
            />
            📄 Pages
          </label>
          <label class="sh-toggle-row">
            <input
              type="checkbox"
              checked={featureCalendar.value}
              onChange={(e) => {
                featureCalendar.value = (e.target as HTMLInputElement).checked
              }}
            />
            🗓 Calendar
          </label>
          <label class="sh-toggle-row">
            <input
              type="checkbox"
              checked={featureTodo.value}
              onChange={(e) => {
                featureTodo.value = (e.target as HTMLInputElement).checked
              }}
            />
            ✅ Tasks
          </label>
          <label class="sh-toggle-row">
            <input
              type="checkbox"
              checked={featureStickies.value}
              onChange={(e) => {
                featureStickies.value = (e.target as HTMLInputElement).checked
              }}
            />
            📝 Stickies
          </label>
          <label class="sh-toggle-row">
            <input
              type="checkbox"
              checked={featureGallery.value}
              onChange={(e) => {
                featureGallery.value = (e.target as HTMLInputElement).checked
              }}
            />
            🖼 Gallery
          </label>
          <label class="sh-toggle-row">
            <input
              type="checkbox"
              checked={featureBazaar.value}
              onChange={(e) => {
                featureBazaar.value = (e.target as HTMLInputElement).checked
              }}
            />
            🛍 Bazaar
          </label>
        </fieldset>
        <fieldset class="sh-form-fieldset" data-testid="space-post-types">
          <legend>📮 Post types</legend>
          <p class="sh-muted" style={{ marginTop: 0 }}>
            Choose which kinds of posts members can create in this feed.
            Turning one off hides it from the composer; existing posts of
            that type stay visible.
          </p>
          {SPACE_POST_TYPES.map(([key, label]) => (
            <label key={key} class="sh-toggle-row">
              <input
                type="checkbox"
                checked={postTypeEnabled.value[key]}
                onChange={(e) => {
                  postTypeEnabled.value = {
                    ...postTypeEnabled.value,
                    [key]: (e.target as HTMLInputElement).checked,
                  }
                }}
              />
              {label}
            </label>
          ))}
        </fieldset>

        <fieldset class="sh-form-fieldset">
          <legend>📍 Location sharing</legend>
          <label class="sh-toggle-row">
            <input
              type="checkbox"
              checked={locationEnabled.value}
              onChange={(e) => {
                locationEnabled.value = (e.target as HTMLInputElement).checked
              }}
            />
            Show a map tab to members of this space
          </label>
          {locationEnabled.value && (
            <>
              <fieldset class="sh-mode-fieldset" aria-label="Privacy mode">
                <legend>Privacy mode</legend>
                <label class={`sh-mode-option ${locationMode.value === 'gps' ? 'sh-mode-option--selected' : ''}`}>
                  <input
                    type="radio"
                    name={`location-mode-${space.id}`}
                    value="gps"
                    checked={locationMode.value === 'gps'}
                    onChange={() => { locationMode.value = 'gps' }}
                  />
                  <span class="sh-mode-option__body">
                    <span class="sh-mode-option__title">
                      🛰️ Live GPS
                    </span>
                    <span class="sh-muted">
                      Opted-in members broadcast their GPS to the space.
                      Coordinates are rounded to ~10 m before they leave
                      your home server.
                    </span>
                  </span>
                </label>
                <label class={`sh-mode-option ${locationMode.value === 'zone_only' ? 'sh-mode-option--selected' : ''}`}>
                  <input
                    type="radio"
                    name={`location-mode-${space.id}`}
                    value="zone_only"
                    checked={locationMode.value === 'zone_only'}
                    onChange={() => { locationMode.value = 'zone_only' }}
                  />
                  <span class="sh-mode-option__body">
                    <span class="sh-mode-option__title">
                      🔒 Zone only
                      <span class="sh-mode-option__badge">stronger privacy</span>
                    </span>
                    <span class="sh-muted">
                      Your home server matches each member's GPS to a
                      space-defined zone and sends only the zone label.
                      Raw coordinates never leave your household. Members
                      outside every zone show nothing.
                    </span>
                  </span>
                </label>
              </fieldset>
              <p class="sh-muted">
                <a href={`/spaces/${space.id}/zones`}>Manage zones →</a>
                {locationMode.value === 'zone_only'
                  && ' (required for zone-only mode)'}
              </p>
            </>
          )}
          <p class="sh-muted">
            HA-defined zone names are never sent to a space, regardless
            of mode. Per-space zones (managed above) are the only labels
            ever shared.
          </p>
        </fieldset>

        {/* Followers (subscribers) are read-only by default. Admins can
         *  open one or both engagement paths so a follower-shaped
         *  audience (extended family, alumni, peers) can leave a 👍 or
         *  drop a comment without being promoted to a full member.
         *  Posting top-level content stays member-only. */}
        <fieldset class="sh-form-fieldset">
          <legend>🔔 Followers</legend>
          <p class="sh-muted" style={{ marginTop: 0 }}>
            Anyone who follows this space sees new posts but is read-only
            by default. Loosen that here without making them full members.
          </p>
          <label>
            <input
              type="checkbox"
              checked={allowSubscriberReact.value}
              onChange={(e) => {
                allowSubscriberReact.value =
                  (e.target as HTMLInputElement).checked
              }}
            />
            Let followers leave reactions
          </label>
          <label>
            <input
              type="checkbox"
              checked={allowSubscriberComment.value}
              onChange={(e) => {
                allowSubscriberComment.value =
                  (e.target as HTMLInputElement).checked
              }}
            />
            Let followers comment on posts
          </label>
          <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
            Posting (text, images, polls, etc.) always stays member-only.
          </p>
        </fieldset>

        <div class="sh-form-actions">
          <Button onClick={save}>Save changes</Button>
        </div>
      </div>

      <hr />
      <h3>{t('space.federation')}</h3>
      {federationLoading.value ? (
        <p class="sh-muted">{t('common.loading')}</p>
      ) : gfsServers.value.length === 0 ? (
        <p class="sh-muted">{t('space.no_gfs_connections')}</p>
      ) : (
        <div class="sh-federation-list">
          {gfsServers.value.map(gfs => {
            const published = isPublished(gfs.id)
            const publicUrl = publicSpaceUrl(gfs.inbox_url, space.id)
            return (
              <div key={gfs.id} class="sh-federation-row">
                <div class="sh-connection-info">
                  <span class={`sh-status-dot sh-status-dot--${gfs.status === 'active' ? 'active' : gfs.status === 'suspended' ? 'unreachable' : 'pending'}`} />
                  <strong>{gfs.display_name}</strong>
                  <span class="sh-muted">{gfs.inbox_url}</span>
                </div>
                {published && (
                  <div class="sh-federation-public-url">
                    <span class="sh-muted">🔗 Public link</span>
                    <a
                      href={publicUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      class="sh-federation-public-url__link"
                      title={publicUrl}
                    >
                      {publicUrl}
                    </a>
                    <button
                      type="button"
                      class="sh-federation-public-url__copy"
                      onClick={() => void copyToClipboard(publicUrl)}
                      aria-label="Copy public link to clipboard"
                      title="Copy"
                    >
                      📋
                    </button>
                  </div>
                )}
                <div class="sh-federation-actions">
                  <span class={published ? 'sh-text-success' : 'sh-muted'}>
                    {published ? t('space.published') : t('space.not_published')}
                  </span>
                  <Button
                    variant={published ? 'danger' : 'primary'}
                    onClick={() => togglePublish(space.id, gfs.id)}
                  >
                    {published ? t('gfs.unpublish') : t('gfs.publish')}
                  </Button>
                </div>
              </div>
            )
          })}
        </div>
      )}

      <hr />
      <h3>Archive</h3>
      {space.archived ? (
        <>
          <p class="sh-muted" style={{ marginTop: 0 }}>
            This space is <strong>archived</strong>: it's read-only and hidden
            from your active spaces. Everything is kept — unarchive to use it
            again.
          </p>
          <Button variant="secondary" onClick={() => setArchived(false)}>Unarchive space</Button>
        </>
      ) : (
        <>
          <p class="sh-muted" style={{ marginTop: 0 }}>
            Hide this space and make it read-only without deleting anything.
            Reversible at any time.
          </p>
          <Button variant="secondary" onClick={() => setArchived(true)}>Archive space</Button>
        </>
      )}

      <hr />
      <h3>Danger zone</h3>
      <Button variant="danger" onClick={() => showDissolve.value = true}>Dissolve space</Button>
      <ConfirmDialog open={showDissolve.value} title="Dissolve space?"
        message="This permanently deletes the space and all its content — posts, photos, events, everything — for every member household. This cannot be undone. To just hide it, use Archive instead."
        confirmLabel="Dissolve" destructive
        onConfirm={() => { showDissolve.value = false; dissolve() }}
        onCancel={() => showDissolve.value = false} />
    </div>
  )
}
