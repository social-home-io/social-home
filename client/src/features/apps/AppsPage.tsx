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
  appsLoading,
  catalogLoading,
  appsError,
  catalogError,
  loadInstalled,
  loadCatalog,
  installApp,
  uninstallApp,
  setEnabled,
  type InstalledApp,
  type CatalogEntry,
} from '@/store/apps'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { showToast } from '@/components/Toast'
import { ApiError } from '@/api'

export default function AppsPage() {
  useTitle('Apps')
  const isAdmin = !!currentUser.value?.is_admin

  useEffect(() => {
    void loadInstalled()
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
  const loading = appsLoading.value
  const error   = appsError.value
  const apps    = installedApps.value

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

  return (
    <section class="sh-apps-section">
      <h2 class="sh-apps-section__title">Installed</h2>
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
            <AppCard key={app.app_id} app={app} isAdmin={isAdmin} />
          ))}
        </div>
      )}
    </section>
  )
}

function AppCard({ app, isAdmin }: { app: InstalledApp; isAdmin: boolean }) {
  const [uninstallOpen, setUninstallOpen] = useState(false)
  const [togglingEnabled, setTogglingEnabled] = useState(false)
  const [uninstalling, setUninstalling]   = useState(false)

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

  return (
    <div class="sh-welcome-card sh-app-card">
      <div class="sh-app-card__header">
        {app.icon ? (
          <img src={app.icon} alt="" class="sh-app-card__icon" aria-hidden="true" />
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

      {app.capabilities.length > 0 && (
        <div class="sh-app-card__caps">
          {app.capabilities.map(cap => (
            <span key={cap} class="sh-chip sh-chip--muted">{cap}</span>
          ))}
        </div>
      )}

      {isAdmin && (
        <div class="sh-app-card__actions">
          <Button
            variant="danger"
            onClick={() => setUninstallOpen(true)}
          >
            Uninstall
          </Button>
        </div>
      )}

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
        {entry.icon_url ? (
          <img src={entry.icon_url} alt="" class="sh-app-card__icon" aria-hidden="true" />
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
