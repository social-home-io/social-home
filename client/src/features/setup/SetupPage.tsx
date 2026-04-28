import { useEffect, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { setToken } from '@/store/auth'
import { instanceConfig, loadInstanceConfig } from '@/store/instance'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'

interface HaPerson {
  username: string
  display_name: string
  picture_url: string | null
}

const haPersons = signal<HaPerson[] | null>(null)
const haPersonsError = signal<string | null>(null)

async function fetchHaPersons(): Promise<HaPerson[]> {
  if (haPersons.value) return haPersons.value
  try {
    const resp = await api.get('/api/setup/ha/persons') as { persons: HaPerson[] }
    haPersons.value = resp.persons
    return resp.persons
  } catch (err: any) {
    haPersonsError.value = err?.message || 'Failed to load HA persons.'
    throw err
  }
}

/**
 * SetupPage — first-boot wizard.
 *
 * Mode-aware: standalone shows a username+password form, ha shows a
 * person-pick + password form, haos auto-completes silently. The
 * caller (App.tsx) only renders this when
 * `instanceConfig.value?.setup_required === true`.
 */
export function SetupPage() {
  const cfg = instanceConfig.value
  if (!cfg) {
    // App.tsx fetches before rendering, so this is defensive only.
    return <div class="sh-setup-loading">{t('setup.loading')}</div>
  }
  if (cfg.mode === 'haos') return <HaosAutoComplete />
  if (cfg.mode === 'ha') return <HaOwnerForm />
  return <StandaloneSetupForm />
}

// ── Standalone: username + password ─────────────────────────────────────────

function StandaloneSetupForm() {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: Event) {
    e.preventDefault()
    if (password !== confirm) {
      setError(t('setup.error.password_mismatch'))
      return
    }
    if (password.length < 8) {
      setError(t('setup.error.password_too_short'))
      return
    }
    setBusy(true)
    setError(null)
    try {
      const resp = await api.post('/api/setup/standalone', { username, password }) as
        { token: string }
      setToken(resp.token)
      // Refresh the instance config so setup_required flips to false
      // and the SPA stops redirecting here.
      await loadInstanceConfig()
      showToast(t('setup.success'), 'success')
      window.location.href = '/'
    } catch (err: any) {
      setError(err?.message || t('setup.error.generic'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div class="sh-setup" role="main">
      <h1>{t('setup.standalone.title')}</h1>
      <p>{t('setup.standalone.intro')}</p>
      <form onSubmit={submit} class="sh-setup-form">
        <label>
          {t('setup.username')}
          <input
            name="username"
            type="text"
            autoComplete="username"
            required
            value={username}
            onInput={(e) => setUsername((e.target as HTMLInputElement).value)}
          />
        </label>
        <label>
          {t('setup.password')}
          <input
            name="password"
            type="password"
            autoComplete="new-password"
            required
            value={password}
            onInput={(e) => setPassword((e.target as HTMLInputElement).value)}
          />
        </label>
        <label>
          {t('setup.password_confirm')}
          <input
            name="password_confirm"
            type="password"
            autoComplete="new-password"
            required
            value={confirm}
            onInput={(e) => setConfirm((e.target as HTMLInputElement).value)}
          />
        </label>
        <FormError id="setup-error" message={error} />
        <Button type="submit" disabled={busy}>
          {busy ? t('setup.submitting') : t('setup.submit')}
        </Button>
      </form>
    </div>
  )
}

// ── ha: pick HA person + password ───────────────────────────────────────────

function HaOwnerForm() {
  const [persons, setPersons] = useState<HaPerson[] | null>(haPersons.value)
  const [loading, setLoading] = useState(persons === null)
  const [picked, setPicked] = useState<string | null>(null)
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (persons !== null) return
    fetchHaPersons().then(
      (p) => { setPersons(p); setLoading(false) },
      () => { setLoading(false) },
    )
  }, [])

  async function submit(e: Event) {
    e.preventDefault()
    if (!picked) {
      setError(t('setup.error.no_person'))
      return
    }
    if (password !== confirm) {
      setError(t('setup.error.password_mismatch'))
      return
    }
    if (password.length < 8) {
      setError(t('setup.error.password_too_short'))
      return
    }
    setBusy(true)
    setError(null)
    try {
      const resp = await api.post('/api/setup/ha/owner', {
        username: picked, password,
      }) as { token: string }
      setToken(resp.token)
      await loadInstanceConfig()
      showToast(t('setup.success'), 'success')
      window.location.href = '/'
    } catch (err: any) {
      setError(err?.message || t('setup.error.generic'))
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <div class="sh-setup-loading">{t('setup.loading')}</div>
  if (haPersonsError.value) {
    return (
      <div class="sh-setup-error">
        <h1>{t('setup.ha.title')}</h1>
        <FormError id="setup-error" message={haPersonsError.value} />
      </div>
    )
  }
  if (!persons || persons.length === 0) {
    return (
      <div class="sh-setup" role="main">
        <h1>{t('setup.ha.title')}</h1>
        <p>{t('setup.ha.no_persons')}</p>
      </div>
    )
  }

  return (
    <div class="sh-setup" role="main">
      <h1>{t('setup.ha.title')}</h1>
      <p>{t('setup.ha.intro')}</p>
      <form onSubmit={submit} class="sh-setup-form">
        <fieldset class="sh-setup-persons">
          <legend>{t('setup.ha.pick_owner')}</legend>
          {persons.map((p) => (
            <label key={p.username} class="sh-setup-person">
              <input
                type="radio"
                name="picked"
                value={p.username}
                checked={picked === p.username}
                onChange={() => setPicked(p.username)}
              />
              {p.picture_url && (
                <img src={p.picture_url} alt="" class="sh-setup-avatar" />
              )}
              <span>{p.display_name}</span>
            </label>
          ))}
        </fieldset>
        <label>
          {t('setup.password')}
          <input
            type="password"
            autoComplete="new-password"
            required
            value={password}
            onInput={(e) => setPassword((e.target as HTMLInputElement).value)}
          />
        </label>
        <label>
          {t('setup.password_confirm')}
          <input
            type="password"
            autoComplete="new-password"
            required
            value={confirm}
            onInput={(e) => setConfirm((e.target as HTMLInputElement).value)}
          />
        </label>
        <FormError id="setup-error" message={error} />
        <Button type="submit" disabled={busy}>
          {busy ? t('setup.submitting') : t('setup.submit')}
        </Button>
      </form>
    </div>
  )
}

// ── haos: silent auto-complete via Supervisor ──────────────────────────────

function HaosAutoComplete() {
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    api.post('/api/setup/haos/complete').then(
      async () => {
        if (cancelled) return
        await loadInstanceConfig()
        // Ingress already provides the auth headers, so we don't need
        // a token — bounce straight into the app.
        window.location.href = '/'
      },
      (err) => {
        if (cancelled) return
        setError(err?.message || t('setup.error.generic'))
      },
    )
    return () => { cancelled = true }
  }, [])

  return (
    <div class="sh-setup-loading" role="main">
      <h1>{t('setup.haos.title')}</h1>
      {error
        ? <FormError id="setup-error" message={error} />
        : <p>{t('setup.haos.completing')}</p>}
    </div>
  )
}

export default SetupPage
