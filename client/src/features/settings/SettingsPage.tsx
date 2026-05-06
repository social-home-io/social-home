import { useEffect } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import { signal } from '@preact/signals'
import { currentUser } from '@/store/auth'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import type { User } from '@/types'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { theme, type Theme } from '@/store/theme'
import { HouseholdThemeStudio } from '@/components/HouseholdThemeStudio'
import { locale, setLocale } from '@/i18n/i18n'
import localeMeta from '@/i18n/locales/_meta.json'
import {
  getLandingPath,
  setPreference,
  type LandingPath,
} from '@/utils/preferences'
import { confirmDialog } from '@/components/confirm'
import { blockedUsers, loadBlocks, unblockUser } from '@/store/blocks'
import { followedUsers, loadFollows, unfollowUser } from '@/store/follows'

type SettingsTab = 'profile' | 'privacy' | 'notifications' | 'appearance'

const activeTab = signal<SettingsTab>('profile')
const displayName = signal('')
const bio = signal('')
const landingPath = signal<LandingPath>('/')
const avatarUrl = signal<string | null>(null)
const onlineStatusVisible = signal(true)
const pushEnabled = signal(
  typeof Notification !== 'undefined' ? Notification.permission === 'granted' : false
)

/** Read ``online_status_visible`` from the cached
 *  ``currentUser.preferences_json``. The previous implementation
 *  GET'd a non-existent ``/api/me/privacy`` endpoint, hard-failed
 *  the route after PR #126's load-error chip surfaced the 404, and
 *  showed every user a "Couldn't load your privacy settings" panel.
 *  Privacy is just a user preference (same store as Highlights prefs);
 *  read it inline from the user we already loaded on cold start. */
function syncOnlineStatusFromUser(): void {
  const raw = (currentUser.value as unknown as { preferences_json?: string } | null)
    ?.preferences_json
  if (!raw) return
  try {
    const prefs = JSON.parse(raw) as { online_status_visible?: boolean }
    if (typeof prefs.online_status_visible === 'boolean') {
      onlineStatusVisible.value = prefs.online_status_visible
    }
  } catch { /* keep the default */ }
}

export default function SettingsPage() {
  useTitle('Settings')
  useEffect(() => {
    if (currentUser.value) {
      displayName.value = currentUser.value.display_name
      bio.value = currentUser.value.bio || ''
      avatarUrl.value = currentUser.value.picture_url
    }
    landingPath.value = getLandingPath()
    syncOnlineStatusFromUser()
  }, [])

  const panelId = (t: SettingsTab) => `sh-settings-panel-${t}`
  const tabId   = (t: SettingsTab) => `sh-settings-tab-${t}`

  return (
    <div class="sh-settings">
      <nav class="sh-settings-tabs" role="tablist">
        {(['profile', 'privacy', 'notifications', 'appearance'] as SettingsTab[]).map(t => (
          <button
            key={t}
            type="button"
            role="tab"
            id={tabId(t)}
            aria-selected={activeTab.value === t}
            aria-controls={panelId(t)}
            tabIndex={activeTab.value === t ? 0 : -1}
            class={activeTab.value === t ? 'sh-tab sh-tab--active' : 'sh-tab'}
            onClick={() => { activeTab.value = t }}
          >
            {t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
      </nav>

      <div role="tabpanel" id={panelId('profile')} aria-labelledby={tabId('profile')} hidden={activeTab.value !== 'profile'}>
        {activeTab.value === 'profile' && <ProfileTab />}
      </div>
      <div role="tabpanel" id={panelId('privacy')} aria-labelledby={tabId('privacy')} hidden={activeTab.value !== 'privacy'}>
        {activeTab.value === 'privacy' && <PrivacyTab />}
      </div>
      <div role="tabpanel" id={panelId('notifications')} aria-labelledby={tabId('notifications')} hidden={activeTab.value !== 'notifications'}>
        {activeTab.value === 'notifications' && <NotificationsTab />}
      </div>
      <div role="tabpanel" id={panelId('appearance')} aria-labelledby={tabId('appearance')} hidden={activeTab.value !== 'appearance'}>
        {activeTab.value === 'appearance' && <AppearanceTab />}
      </div>
    </div>
  )
}

function ProfileTab() {
  const refresh = async () => {
    try {
      const me = await api.get('/api/me') as User
      displayName.value = me.display_name
      bio.value = me.bio ?? ''
      avatarUrl.value = me.picture_url
      // Mirror the fresh user onto the auth store so other surfaces
      // (sidenav, profile card, post avatars built from currentUser)
      // pick up the new ``picture_url`` immediately AND so leaving and
      // returning to this page doesn't reset to a stale signed URL
      // from the original ``loadCurrentUser`` fetch.
      if (currentUser.value) {
        currentUser.value = { ...currentUser.value, ...me }
      }
    } catch { /* noop */ }
  }

  const handleSave = async (e: Event) => {
    e.preventDefault()
    try {
      await api.patch('/api/me', {
        display_name: displayName.value,
        bio: bio.value || null,
      })
      showToast('Settings saved', 'success')
    } catch (err: unknown) {
      showToast(
        `Save failed: ${(err as Error).message ?? err}`, 'error',
      )
    }
  }

  const handleAvatarUpload = async (e: Event) => {
    const input = e.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return
    const fd = new FormData()
    fd.append('file', file)
    try {
      await api.upload('/api/me/picture', fd)
      await refresh()
      showToast('Avatar updated', 'success')
    } catch (err: unknown) {
      showToast(
        `Avatar upload failed: ${(err as Error).message ?? err}`, 'error',
      )
    }
    input.value = ''
  }

  const handleAvatarClear = async () => {
    if (!await confirmDialog('Remove your profile picture?', { destructive: true })) return
    try {
      await api.delete('/api/me/picture')
      await refresh()
      showToast('Avatar removed', 'info')
    } catch (err: unknown) {
      showToast(
        `Clear failed: ${(err as Error).message ?? err}`, 'error',
      )
    }
  }

  const handleUseHaPicture = async () => {
    try {
      await api.post('/api/me/picture/refresh-from-ha', {})
      await refresh()
      showToast('Synced picture from Home Assistant', 'success')
    } catch (err: unknown) {
      showToast(
        `Could not fetch from HA: ${(err as Error).message ?? err}`,
        'error',
      )
    }
  }

  const isHaUser = currentUser.value?.source === 'ha'
  const bioRemaining = 300 - bio.value.length

  return (
    <section class="sh-settings-section">
      <h2>Profile</h2>
      <div class="sh-profile-card">
        <label class="sh-profile-avatar-slot"
               title="Click or drop an image to change your avatar">
          <Avatar name={displayName.value || '?'} src={avatarUrl.value}
                  size={112} />
          <span class="sh-profile-avatar-hint" aria-hidden="true">
            📷 Change
          </span>
          <input type="file" accept="image/*"
                 onChange={handleAvatarUpload} hidden />
        </label>
        <div class="sh-profile-card-meta">
          <div class="sh-profile-identity">
            <strong class="sh-profile-name">
              {displayName.value || '—'}
            </strong>
            <span class="sh-muted">@{currentUser.value?.username}</span>
          </div>
          <span class={`sh-profile-source sh-profile-source--${isHaUser ? 'ha' : 'manual'}`}>
            {isHaUser ? '🏠 Synced from Home Assistant' : '✏️ Set manually'}
          </span>
          <div class="sh-row" style={{ gap: 'var(--sh-space-xs)', flexWrap: 'wrap' }}>
            {avatarUrl.value && (
              <Button variant="secondary" onClick={handleAvatarClear}>
                Remove picture
              </Button>
            )}
            {isHaUser && (
              <Button variant="secondary" onClick={handleUseHaPicture}>
                Use Home Assistant picture
              </Button>
            )}
          </div>
        </div>
      </div>
      <form class="sh-form" onSubmit={handleSave}>
        <label>
          Display name
          <input value={displayName.value} maxLength={64}
                 onInput={(e) => displayName.value = (e.target as HTMLInputElement).value} />
        </label>
        <label>
          Bio
          <textarea value={bio.value} maxLength={300} rows={3}
                    onInput={(e) => bio.value = (e.target as HTMLTextAreaElement).value} />
          <span class="sh-char-count">
            {bioRemaining} characters left
          </span>
        </label>
        <div class="sh-form-actions">
          <Button type="submit">Save profile</Button>
        </div>
      </form>

      <LandingPicker />
    </section>
  )
}

function LandingPicker() {
  const handleChange = async (choice: LandingPath) => {
    const prev = landingPath.value
    landingPath.value = choice
    try {
      await setPreference('landing_path', choice)
      showToast(
        choice === '/dashboard'
          ? 'Landing page set to My Corner'
          : 'Landing page set to the feed',
        'success',
      )
    } catch (err: unknown) {
      landingPath.value = prev
      showToast(
        `Could not save: ${(err as Error).message ?? err}`, 'error',
      )
    }
  }

  return (
    <div class="sh-landing-picker">
      <h3>Home page</h3>
      <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)', margin: 0 }}>
        Which page opens when you tap the Social Home logo.
      </p>
      <div class="sh-landing-picker-options" role="radiogroup"
           aria-label="Landing page">
        <label class={`sh-landing-option ${landingPath.value === '/' ? 'sh-landing-option--active' : ''}`}>
          <input type="radio" name="landing" value="/"
                 checked={landingPath.value === '/'}
                 onChange={() => void handleChange('/')} />
          <span class="sh-landing-option-icon">📰</span>
          <span class="sh-landing-option-body">
            <strong>Household feed</strong>
            <span class="sh-muted">Posts, photos, conversations</span>
          </span>
        </label>
        <label class={`sh-landing-option ${landingPath.value === '/dashboard' ? 'sh-landing-option--active' : ''}`}>
          <input type="radio" name="landing" value="/dashboard"
                 checked={landingPath.value === '/dashboard'}
                 onChange={() => void handleChange('/dashboard')} />
          <span class="sh-landing-option-icon">🏠</span>
          <span class="sh-landing-option-body">
            <strong>My Corner</strong>
            <span class="sh-muted">Tasks, events, notifications at a glance</span>
          </span>
        </label>
      </div>
    </div>
  )
}

function PrivacyTab() {
  const toggleOnlineStatus = async () => {
    onlineStatusVisible.value = !onlineStatusVisible.value
    try {
      // Privacy preferences ride on the existing
      // ``users.preferences_json`` blob — same store ``HighlightsPrefs``
      // uses. PATCH /api/me with a ``preferences`` patch shallow-merges
      // the keys, so unrelated prefs are untouched.
      const updated = await api.patch('/api/me', {
        preferences: { online_status_visible: onlineStatusVisible.value },
      }) as { preferences_json?: string }
      // Mirror the server's authoritative blob onto the auth store so
      // a tab switch / reload reads the same value without an extra
      // /api/me round-trip.
      if (currentUser.value && updated.preferences_json) {
        currentUser.value = {
          ...currentUser.value,
          preferences_json: updated.preferences_json,
        } as User
      }
      showToast('Privacy updated', 'success')
    } catch {
      onlineStatusVisible.value = !onlineStatusVisible.value
      showToast('Failed to update privacy', 'error')
    }
  }

  return (
    <section class="sh-settings-section">
      <h2>Privacy</h2>
      <label class="sh-toggle-row">
        <input
          type="checkbox"
          checked={onlineStatusVisible.value}
          onChange={toggleOnlineStatus}
        />
        Show online status to other household members
      </label>
      <HighlightsPreferencesPanel />
      <BlockedAccountsPanel />
      <FollowingPanel />
    </section>
  )
}


function FollowingPanel() {
  useEffect(() => {
    void loadFollows(true)
  }, [])
  const rows = followedUsers.value

  const onUnfollow = async (userId: string) => {
    if (!await confirmDialog(
      `Unfollow ${userId}? Their moments older than 24 hours will stop `
      + `surfacing in your inbox.`,
      { confirmLabel: 'Unfollow' },
    )) return
    try {
      await unfollowUser(userId)
      showToast('Unfollowed', 'success')
    } catch (e: unknown) {
      showToast(`Couldn't unfollow: ${(e as Error)?.message ?? e}`, 'error')
    }
  }

  return (
    <div class="sh-following">
      <h3 style={{ marginBottom: 0 }}>Following</h3>
      <p class="sh-muted" style={{ marginTop: '0.25rem' }}>
        Following someone extends the moments retention window from 24
        hours to 7 days for their posts in your inbox.
      </p>
      {rows.length === 0 && (
        <p class="sh-muted" style={{ marginTop: '0.25rem' }}>
          You aren't following anyone. Tap a moment author's name and
          choose Follow to start.
        </p>
      )}
      {rows.length > 0 && (
        <ul class="sh-following-list" aria-label="Following">
          {rows.map(f => (
            <li key={f.user_id} class="sh-following-row">
              <Avatar name={f.user_id} size={32} />
              <span class="sh-following-meta">
                <strong>{f.user_id}</strong>
                <span class="sh-muted">
                  Following since {new Date(f.created_at).toLocaleDateString()}
                </span>
              </span>
              <Button
                variant="secondary"
                onClick={() => void onUnfollow(f.user_id)}
              >
                Unfollow
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}


function BlockedAccountsPanel() {
  useEffect(() => {
    void loadBlocks(true)
  }, [])
  const rows = blockedUsers.value

  const onUnblock = async (userId: string) => {
    if (!await confirmDialog(
      `Unblock ${userId}? Their highlights, posts, presence and DMs will be `
      + `visible to you again.`,
      { confirmLabel: 'Unblock' },
    )) return
    try {
      await unblockUser(userId)
      showToast('Unblocked', 'success')
    } catch (e: unknown) {
      showToast(`Couldn't unblock: ${(e as Error)?.message ?? e}`, 'error')
    }
  }

  return (
    <div class="sh-blocked-accounts">
      <h3 style={{ marginBottom: 0 }}>Blocked accounts</h3>
      {rows.length === 0 && (
        <p class="sh-muted" style={{ marginTop: '0.25rem' }}>
          You haven't blocked anyone. Open a highlight or profile and tap
          the ⋯ menu to block someone.
        </p>
      )}
      {rows.length > 0 && (
        <ul class="sh-blocked-accounts-list" aria-label="Blocked accounts">
          {rows.map(b => (
            <li key={b.user_id} class="sh-blocked-accounts-row">
              <Avatar name={b.user_id} size={32} />
              <span class="sh-blocked-accounts-meta">
                <strong>{b.user_id}</strong>
                <span class="sh-muted">
                  Blocked {new Date(b.blocked_at).toLocaleDateString()}
                </span>
              </span>
              <Button
                variant="secondary"
                onClick={() => void onUnblock(b.user_id)}
              >
                Unblock
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

interface HighlightsPrefs {
  retention_days?: number
  max_count?: number
  default_audience?: { kind: 'all_paired' | 'households' | 'users' }
}

function HighlightsPreferencesPanel() {
  // Read straight from the cached preferences each render — the
  // `setPreference` helper updates the cache in place, so the form
  // re-renders with the latest values when the user saves.
  const prefs = (() => {
    try {
      const raw = (currentUser.value as unknown as { preferences_json?: string } | null)
        ?.preferences_json
      if (!raw) return {} as HighlightsPrefs
      const parsed = JSON.parse(raw) as { highlights?: HighlightsPrefs }
      return parsed.highlights ?? {}
    } catch { return {} as HighlightsPrefs }
  })()
  const retentionDays = signal<number>(prefs.retention_days ?? 30)
  const maxCount = signal<number>(prefs.max_count ?? 100)
  const audienceKind = signal<'all_paired' | 'households' | 'users'>(
    prefs.default_audience?.kind ?? 'all_paired',
  )

  const save = async () => {
    try {
      await setPreference('highlights', {
        retention_days: retentionDays.value,
        max_count: maxCount.value,
        default_audience: { kind: audienceKind.value },
      })
      showToast('Highlights settings saved', 'success')
    } catch {
      showToast('Failed to save highlights settings', 'error')
    }
  }

  return (
    <div id="highlights" class="sh-settings-highlights-panel">
      <h3>Highlights</h3>
      <p class="sh-muted">
        Control how long your highlights stay listed and who sees them by default.
      </p>
      <label class="sh-form-row">
        Retention (days)
        <input
          type="number"
          min={1}
          max={90}
          value={retentionDays.value}
          onInput={e => {
            const n = Number((e.target as HTMLInputElement).value)
            retentionDays.value = Number.isFinite(n) ? n : 30
          }}
        />
      </label>
      <label class="sh-form-row">
        Max highlights to keep
        <input
          type="number"
          min={10}
          max={500}
          value={maxCount.value}
          onInput={e => {
            const n = Number((e.target as HTMLInputElement).value)
            maxCount.value = Number.isFinite(n) ? n : 100
          }}
        />
      </label>
      <label class="sh-form-row">
        Default audience
        <select
          value={audienceKind.value}
          onChange={e => {
            const v = (e.target as HTMLSelectElement).value as
              'all_paired' | 'households' | 'users'
            audienceKind.value = v
          }}
        >
          <option value="all_paired">All connected households</option>
          <option value="households">Pick households per highlight</option>
          <option value="users">Pick people per highlight (advanced)</option>
        </select>
      </label>
      <div class="sh-form-actions">
        <Button onClick={save}>Save</Button>
      </div>
    </div>
  )
}

function NotificationsTab() {
  const requestPush = async () => {
    if (typeof Notification === 'undefined') return
    const result = await Notification.requestPermission()
    pushEnabled.value = result === 'granted'
    if (result === 'granted') {
      showToast('Push notifications enabled', 'success')
    }
  }

  const disablePush = async () => {
    try {
      const reg = await navigator.serviceWorker.getRegistration()
      const sub = await reg?.pushManager?.getSubscription()
      if (sub) {
        await sub.unsubscribe()
        await api.post('/api/push/unsubscribe', sub.toJSON())
      }
      pushEnabled.value = false
      showToast('Push notifications disabled', 'info')
    } catch {
      showToast('Failed to disable push', 'error')
    }
  }

  return (
    <section class="sh-settings-section">
      <h2>Notifications</h2>
      <div class="sh-settings-row">
        <span>Push notifications</span>
        {pushEnabled.value ? (
          <Button variant="secondary" onClick={disablePush}>Disable</Button>
        ) : (
          <Button onClick={requestPush}>Enable</Button>
        )}
      </div>
      <p class="sh-muted">
        {pushEnabled.value
          ? 'You will receive push notifications for new messages and mentions.'
          : 'Enable push notifications to stay updated when you are away.'}
      </p>
    </section>
  )
}

function AppearanceTab() {
  const setTheme = (t: Theme) => { theme.value = t }

  return (
    <section class="sh-settings-section">
      <h2>Appearance</h2>
      <div class="sh-theme-picker">
        <h3>Theme</h3>
        <div class="sh-theme-options">
          {(['light', 'dark', 'auto'] as Theme[]).map(t => (
            <button
              key={t}
              type="button"
              class={theme.value === t ? 'sh-theme-option sh-theme-option--active' : 'sh-theme-option'}
              onClick={() => setTheme(t)}
            >
              {t === 'light' ? 'Light' : t === 'dark' ? 'Dark' : 'Auto'}
            </button>
          ))}
        </div>
        <p class="sh-muted">
          {theme.value === 'auto'
            ? 'Follows your system preference.'
            : `Currently using ${theme.value} mode.`}
        </p>
      </div>

      <div class="sh-locale-picker">
        <h3>Language</h3>
        <div class="sh-locale-options" role="radiogroup" aria-label="Language">
          {Object.entries(localeMeta.locales).map(([code, info]) => (
            <button
              key={code}
              type="button"
              role="radio"
              aria-checked={locale.value === code}
              class={
                locale.value === code
                  ? 'sh-locale-option sh-locale-option--active'
                  : 'sh-locale-option'
              }
              onClick={() => { void setLocale(code) }}
              title={(info as { english_name: string }).english_name}
            >
              {(info as { native_name: string }).native_name}
            </button>
          ))}
        </div>
        <p class="sh-muted">
          Translations are contributed by the community. Missing or awkward
          text? <a href={localeMeta.weblate_url} target="_blank" rel="noopener noreferrer">
          Contribute translations on Weblate</a>.
        </p>
      </div>
      {currentUser.value?.is_admin && <HouseholdThemeStudio />}
    </section>
  )
}
