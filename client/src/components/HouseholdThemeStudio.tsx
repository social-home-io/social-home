/**
 * HouseholdThemeStudio — household-wide theme + name editor (§23.125).
 *
 * Pairs with SpaceThemeStudio. Hits PUT /api/theme (admin-only) and
 * PUT /api/household/features for the household name. Since the backend
 * accepts PATCH-style partials, each field edit can be saved
 * independently; the UI batches the form into a single Save press.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Button } from './Button'
import { Spinner } from './Spinner'
import { showToast } from './Toast'

type Mode     = 'light' | 'dark' | 'auto'
type FontId   = 'system' | 'serif' | 'rounded' | 'mono'
type Density  = 'compact' | 'comfortable' | 'spacious'

interface HouseholdTheme {
  primary_color: string
  accent_color:  string
  surface_color: string | null
  surface_dark:  string | null
  mode:          Mode
  font_family:   FontId
  density:       Density
  corner_radius: number
}

// Initial signals match the brand defaults in ``tokens.css`` and the
// ``household_theme`` schema row — opening the studio and saving
// without changing the colours leaves the SPA on the warm hearth
// palette instead of flipping ``--sh-primary`` to legacy cold blue.
const primary       = signal('#D2542A')
const accent        = signal('#C8902F')
const surface       = signal<string>('')          // '' = unset
const surfaceDark   = signal<string>('')
const mode          = signal<Mode>('auto')
const font          = signal<FontId>('system')
const density       = signal<Density>('comfortable')
const cornerRadius  = signal<number>(12)
const householdName = signal('Home')
const loading       = signal(true)
const saving        = signal(false)

export function HouseholdThemeStudio() {
  useEffect(() => { void load() }, [])

  if (loading.value) return <Spinner />

  return (
    <section class="sh-household-theme-studio">
      <h3>Household appearance</h3>

      <label>
        Household name
        <input
          type="text"
          maxLength={80}
          value={householdName.value}
          onInput={(e) =>
            (householdName.value = (e.target as HTMLInputElement).value)}
        />
      </label>

      <label>
        Primary colour
        <input
          type="color"
          value={primary.value}
          onInput={(e) => (primary.value = (e.target as HTMLInputElement).value)}
        />
      </label>

      <label>
        Accent colour
        <input
          type="color"
          value={accent.value}
          onInput={(e) => (accent.value = (e.target as HTMLInputElement).value)}
        />
      </label>

      <label>
        Light surface
        <input
          type="color"
          value={surface.value || '#ffffff'}
          onInput={(e) => (surface.value = (e.target as HTMLInputElement).value)}
        />
      </label>

      <label>
        Dark surface
        <input
          type="color"
          value={surfaceDark.value || '#101820'}
          onInput={(e) =>
            (surfaceDark.value = (e.target as HTMLInputElement).value)}
        />
      </label>

      <label>
        Mode
        <select
          value={mode.value}
          onChange={(e) =>
            (mode.value = (e.target as HTMLSelectElement).value as Mode)}
        >
          <option value="auto">Auto (system)</option>
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </select>
      </label>

      <label>
        Font
        <select
          value={font.value}
          onChange={(e) =>
            (font.value = (e.target as HTMLSelectElement).value as FontId)}
        >
          <option value="system">System</option>
          <option value="serif">Serif</option>
          <option value="rounded">Rounded</option>
          <option value="mono">Mono</option>
        </select>
      </label>

      <label>
        Density
        <select
          value={density.value}
          onChange={(e) =>
            (density.value = (e.target as HTMLSelectElement).value as Density)}
        >
          <option value="compact">Compact</option>
          <option value="comfortable">Comfortable</option>
          <option value="spacious">Spacious</option>
        </select>
      </label>

      <label>
        Corner radius ({cornerRadius.value}px)
        <input
          type="range"
          min={0}
          max={24}
          step={1}
          value={cornerRadius.value}
          onInput={(e) =>
            (cornerRadius.value = parseInt(
              (e.target as HTMLInputElement).value, 10,
            ))}
        />
      </label>

      <div
        class="sh-theme-preview"
        style={
          `--sh-primary:${primary.value};` +
          `--sh-accent:${accent.value};` +
          `--sh-radius:${cornerRadius.value}px;` +
          `--sh-surface:${surface.value || 'var(--sh-surface-default)'};`
        }
      >
        <span class="sh-swatch sh-swatch-primary" />
        <span class="sh-swatch sh-swatch-accent" />
      </div>

      <Button onClick={save} disabled={saving.value}>
        {saving.value ? 'Saving…' : 'Save'}
      </Button>
    </section>
  )
}

async function load() {
  loading.value = true
  try {
    const theme = await api.get('/api/theme') as HouseholdTheme
    primary.value      = theme.primary_color
    accent.value       = theme.accent_color
    surface.value      = theme.surface_color ?? ''
    surfaceDark.value  = theme.surface_dark  ?? ''
    mode.value         = theme.mode
    font.value         = theme.font_family
    density.value      = theme.density
    cornerRadius.value = theme.corner_radius
    const feats = await api.get('/api/household/features') as
      { household_name: string }
    householdName.value = feats.household_name
    applyToDocument()
  } catch (err: any) {
    showToast(`Could not load theme: ${err?.message || err}`, 'error')
  } finally {
    loading.value = false
  }
}

// §23.125.4 — write the spec-mandated `--hh-*` custom properties on
// :root so the live page reflects the new theme without a reload.
function applyToDocument() {
  const r = document.documentElement.style
  r.setProperty('--hh-accent',       accent.value)
  if (surface.value)     r.setProperty('--hh-surface',      surface.value)
  if (surfaceDark.value) r.setProperty('--hh-surface-dark', surfaceDark.value)
  const fontMap: Record<FontId, string> = {
    system:  'system-ui, -apple-system, BlinkMacSystemFont, sans-serif',
    serif:   'Georgia, "Times New Roman", serif',
    rounded: '"SF Pro Rounded", "Quicksand", system-ui, sans-serif',
    mono:    'ui-monospace, Menlo, Consolas, monospace',
  }
  r.setProperty('--hh-font', fontMap[font.value])
  r.setProperty('--hh-radius-card', `${cornerRadius.value}px`)
  r.setProperty('--hh-radius-btn',  `${cornerRadius.value}px`)
  const gapMap: Record<Density, string> = {
    compact:     '0.5rem',
    comfortable: '1rem',
    spacious:    '1.5rem',
  }
  r.setProperty('--hh-density-gap', gapMap[density.value])
}

async function save() {
  saving.value = true
  try {
    await api.put('/api/theme', {
      primary_color: primary.value,
      accent_color:  accent.value,
      surface_color: surface.value || null,
      surface_dark:  surfaceDark.value || null,
      mode:          mode.value,
      font_family:   font.value,
      density:       density.value,
      corner_radius: cornerRadius.value,
    })
    await api.put('/api/household/features', {
      household_name: householdName.value.trim() || 'Home',
    })
    applyToDocument()
    showToast('Saved', 'success')
  } catch (err: any) {
    showToast(`Save failed: ${err?.message || err}`, 'error')
  } finally {
    saving.value = false
  }
}
