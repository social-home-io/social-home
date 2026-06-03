/**
 * AppsPage — browse and manage Social Home Apps (§Apps).
 *
 * Two sections:
 *  - **Installed** — visible to all authenticated users; admins also
 *    get enable/disable toggles and an Uninstall button.
 *  - **Browse catalog** — admin-only; lists available apps with an
 *    Install button (already-installed entries are disabled).
 */
import { useEffect, useState } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import { currentUser } from '@/store/auth'
import {
  installedApps,
  catalog,
  updates,
  updatesChecking,
  appsLoading,
  catalogLoading,
  appsError,
  catalogError,
  loadInstalled,
  loadCatalog,
  loadUpdates,
  updateApp,
  installApp,
  uninstallApp,
  setEnabled,
  setMinAge,
  type InstalledApp,
  type AppUpdate,
  type CatalogEntry,
} from '@/store/apps'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { showToast } from '@/components/Toast'
import { ApiError } from '@/api'
import { addBase } from '@/baseUrl'

/**
 * Only render an icon `<img>` for a self-contained `data:` URI or an absolute
 * `https?:` URL. A relative path (e.g. `"icon.svg"`) would resolve against the
 * SPA origin and 404 — render the placeholder instead of a broken image.
 * (App bundles are served from a sandboxed opaque origin, so a relative bundle
 * path is never loadable from a host card anyway.)
 */
export function safeIconSrc(icon: string | null | undefined): string | null {
  if (!icon) return null
  return /^(data:|https?:\/\/)/i.test(icon) ? icon : null
}

export default function AppsPage() {
  useTitle('Apps')
  const isAdmin = !!currentUser.value?.is_admin

  useEffect(() => {
    void loadInstalled()
    void loadUpdates()
    if (isAdmin) void loadCatalog()
  }, [])

  return (
    <div class="sh-page sh-apps-page">
      <header class="sh-page-header">
        <h1 class="sh-page-title">Apps</h1>
      </header>

      <InstalledSection isAdmin={isAdmin} />
      {isAdmin && <CatalogSection />}
    </div>
  )
}

// ─── Installed section ───────────────────────────────────────────────────────

function InstalledSection({ isAdmin }: { isAdmin: boolean }) {
  const loading      = appsLoading.value
  const error        = appsError.value
  const apps         = installedApps.value
  const updateList   = updates.value
  const checking     = updatesChecking.value

  const handleCheckUpdates = async () => {
    await loadUpdates(isAdmin)
    const count = updates.value.length
    showToast(
      count > 0
        ? `${count} update${count === 1 ? '' : 's'} available`
        : 'All apps are up to date',
      'info',
    )
  }

  if (loading) {
    return (
      <section class="sh-apps-section">
        <h2 class="sh-apps-section__title">Installed</h2>
        <div class="sh-apps-loading" aria-live="polite">Loading…</div>
      </section>
    )
  }

  if (error) {
    return (
      <section class="sh-apps-section">
        <h2 class="sh-apps-section__title">Installed</h2>
        <div class="sh-apps-error" role="alert">
          <p>{error}</p>
          <Button onClick={() => { void loadInstalled() }}>Retry</Button>
        </div>
      </section>
    )
  }

  const visible = isAdmin ? apps : apps.filter(a => a.enabled)
  // Build a lookup map: app_id → AppUpdate (only for apps that have updates)
  const updateMap = new Map<string, AppUpdate>(updateList.map(u => [u.app_id, u]))

  return (
    <section class="sh-apps-section">
      <div class="sh-apps-section__header">
        <h2 class="sh-apps-section__title">Installed</h2>
        {isAdmin && (
          <Button
            variant="secondary"
            loading={checking}
            onClick={() => { void handleCheckUpdates() }}
          >
            Check for updates
          </Button>
        )}
      </div>
      {visible.length === 0 ? (
        <div class="sh-apps-empty">
          <p class="sh-muted">No apps installed yet.</p>
          {isAdmin && (
            <p class="sh-muted">
              Browse the catalog below to find and install apps.
            </p>
          )}
        </div>
      ) : (
        <div class="sh-apps-grid">
          {visible.map(app => (
            <AppCard
              key={app.app_id}
              app={app}
              isAdmin={isAdmin}
              update={updateMap.get(app.app_id) ?? null}
            />
          ))}
        </div>
      )}
    </section>
  )
}

const MIN_AGE_OPTIONS: { value: number; label: string }[] = [
  { value: 0,  label: 'Everyone' },
  { value: 13, label: '13+' },
  { value: 16, label: '16+' },
  { value: 18, label: '18+' },
]

function AppCard({
  app,
  isAdmin,
  update,
}: {
  app: InstalledApp
  isAdmin: boolean
  update: AppUpdate | null
}) {
  const [uninstallOpen, setUninstallOpen] = useState(false)
  const [togglingEnabled, setTogglingEnabled] = useState(false)
  const [uninstalling, setUninstalling]   = useState(false)
  const [updating, setUpdating]           = useState(false)
  const [settingMinAge, setSettingMinAge] = useState(false)

  const handleToggle = async () => {
    setTogglingEnabled(true)
    try {
      await setEnabled(app.app_id, !app.enabled)
    } catch (err: unknown) {
      showToast((err as Error).message ?? 'Could not update app.', 'error')
    } finally {
      setTogglingEnabled(false)
    }
  }

  const handleMinAgeChange = async (value: number) => {
    setSettingMinAge(true)
    try {
      await setMinAge(app.app_id, value)
      const label = value === 0 ? 'removed' : `set to ${value}+`
      showToast(`Minimum age ${label}`, 'success')
    } catch (err: unknown) {
      showToast((err as Error).message ?? 'Could not update minimum age.', 'error')
    } finally {
      setSettingMinAge(false)
    }
  }

  const handleUninstall = async () => {
    setUninstalling(true)
    try {
      await uninstallApp(app.app_id)
      showToast(`${app.name} uninstalled.`, 'info')
      setUninstallOpen(false)
    } catch (err: unknown) {
      showToast((err as Error).message ?? 'Could not uninstall app.', 'error')
    } finally {
      setUninstalling(false)
    }
  }

  const handleUpdate = async () => {
    if (!update) return
    setUpdating(true)
    try {
      await updateApp(app.app_id)
      showToast(`Updated ${app.name} to v${update.latest_version}`, 'success')
    } catch (err: unknown) {
      showToast((err as Error).message ?? 'Could not update app.', 'error')
    } finally {
      setUpdating(false)
    }
  }

  return (
    <div class="sh-welcome-card sh-app-card">
      <div class="sh-app-card__header">
        {safeIconSrc(app.icon) ? (
          <img src={safeIconSrc(app.icon)!} alt="" class="sh-app-card__icon" aria-hidden="true" />
        ) : (
          <span class="sh-app-card__icon-placeholder" aria-hidden="true">📦</span>
        )}
        <div class="sh-app-card__meta">
          <span class="sh-app-card__name">{app.name}</span>
          <span class="sh-muted sh-app-card__version">v{app.version}</span>
        </div>
        {isAdmin && (
          <label class="sh-app-card__toggle" aria-label={`${app.enabled ? 'Disable' : 'Enable'} ${app.name}`}>
            <input
              type="checkbox"
              checked={app.enabled}
              disabled={togglingEnabled}
              onChange={() => { void handleToggle() }}
            />
            <span class="sh-app-card__toggle-label">
              {app.enabled ? 'Enabled' : 'Disabled'}
            </span>
          </label>
        )}
      </div>

      {(app.capabilities.length > 0 || app.min_age > 0) && (
        <div class="sh-app-card__caps">
          {app.capabilities.map(cap => (
            <span key={cap} class="sh-chip sh-chip--muted">{cap}</span>
          ))}
          {app.min_age > 0 && (
            <span class="sh-age-chip" title={`Minimum age ${app.min_age}`}>
              {app.min_age}+
            </span>
          )}
        </div>
      )}

      {isAdmin && (
        <div class="sh-app-card__min-age-row">
          <label class="sh-app-card__min-age-label" htmlFor={`min-age-${app.app_id}`}>
            Minimum age
          </label>
          <select
            id={`min-age-${app.app_id}`}
            class="sh-app-card__min-age-select"
            value={app.min_age}
            disabled={settingMinAge}
            onChange={(e) => {
              const val = parseInt((e.target as HTMLSelectElement).value, 10)
              void handleMinAgeChange(val)
            }}
          >
            {MIN_AGE_OPTIONS.map(opt => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </div>
      )}

      {update && (
        <div class="sh-app-card__update-row">
          <span class="sh-chip sh-chip--update" aria-label={`Update available: v${update.latest_version}`}>
            Update available → v{update.latest_version}
          </span>
          {isAdmin && (
            <Button
              variant="primary"
              loading={updating}
              onClick={() => { void handleUpdate() }}
            >
              Update
            </Button>
          )}
        </div>
      )}

      <div class="sh-app-card__actions">
        {app.enabled && (
          <Button
            variant="primary"
            onClick={() => { window.location.href = addBase(`/apps/${encodeURIComponent(app.app_id)}`) }}
          >
            Open
          </Button>
        )}
        {isAdmin && (
          <Button
            variant="danger"
            onClick={() => setUninstallOpen(true)}
          >
            Uninstall
          </Button>
        )}
      </div>

      <Modal
        open={uninstallOpen}
        onClose={() => setUninstallOpen(false)}
        title={`Uninstall ${app.name}?`}
      >
        <p>This will remove <strong>{app.name}</strong> from your household.
           This action cannot be undone.</p>
        <div class="sh-modal-actions">
          <Button variant="secondary" onClick={() => setUninstallOpen(false)}>
            Cancel
          </Button>
          <Button variant="danger" loading={uninstalling} onClick={() => { void handleUninstall() }}>
            Uninstall
          </Button>
        </div>
      </Modal>
    </div>
  )
}

// ─── Catalog / Browse section ─────────────────────────────────────────────────

function CatalogSection() {
  const loading = catalogLoading.value
  const error   = catalogError.value
  const entries = catalog.value
  const installed = installedApps.value

  if (loading) {
    return (
      <section class="sh-apps-section">
        <h2 class="sh-apps-section__title">Browse</h2>
        <div class="sh-apps-loading" aria-live="polite">Loading catalog…</div>
      </section>
    )
  }

  if (error) {
    return (
      <section class="sh-apps-section">
        <h2 class="sh-apps-section__title">Browse</h2>
        <div class="sh-apps-error" role="alert">
          <p>{error}</p>
          <Button onClick={() => { void loadCatalog() }}>Retry</Button>
        </div>
      </section>
    )
  }

  return (
    <section class="sh-apps-section">
      <h2 class="sh-apps-section__title">Browse</h2>
      {entries.length === 0 ? (
        <div class="sh-apps-empty">
          <p class="sh-muted">No apps available in the catalog.</p>
        </div>
      ) : (
        <div class="sh-apps-catalog-list">
          {entries.map(entry => (
            <CatalogRow
              key={entry.app_id}
              entry={entry}
              alreadyInstalled={installed.some(a => a.app_id === entry.app_id)}
            />
          ))}
        </div>
      )}
    </section>
  )
}

function CatalogRow({
  entry,
  alreadyInstalled,
}: {
  entry: CatalogEntry
  alreadyInstalled: boolean
}) {
  const [installing, setInstalling] = useState(false)

  const handleInstall = async () => {
    setInstalling(true)
    try {
      await installApp(entry.app_id)
    } catch (err: unknown) {
      const msg = err instanceof ApiError && err.detail
        ? err.detail
        : (err as Error).message ?? 'Could not install app.'
      showToast(msg, 'error')
    } finally {
      setInstalling(false)
    }
  }

  return (
    <div class="sh-welcome-card sh-catalog-row">
      <div class="sh-catalog-row__header">
        {safeIconSrc(entry.icon_url) ? (
          <img src={safeIconSrc(entry.icon_url)!} alt="" class="sh-app-card__icon" aria-hidden="true" />
        ) : (
          <span class="sh-app-card__icon-placeholder" aria-hidden="true">📦</span>
        )}
        <div class="sh-catalog-row__meta">
          <span class="sh-catalog-row__name">{entry.name}</span>
          <span class="sh-muted">v{entry.latest_version}</span>
        </div>
        {alreadyInstalled ? (
          <span class="sh-chip sh-chip--success">Installed</span>
        ) : (
          <Button
            variant="primary"
            loading={installing}
            onClick={() => { void handleInstall() }}
          >
            Install
          </Button>
        )}
      </div>
      {entry.description && (
        <p class="sh-catalog-row__desc sh-muted">{entry.description}</p>
      )}
      {entry.capabilities.length > 0 && (
        <div class="sh-app-card__caps">
          {entry.capabilities.map(cap => (
            <span key={cap} class="sh-chip sh-chip--muted">{cap}</span>
          ))}
        </div>
      )}
    </div>
  )
}
