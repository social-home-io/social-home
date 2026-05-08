/**
 * SpaceThemeStudio — per-space theming studio (§23.123 / §23.124).
 *
 * Mirrors the :mod:`HouseholdThemeStudio` layout at space scope:
 * quick-apply preset swatches, primary + accent color pickers, a
 * background-tint picker, mode override, font family, post layout.
 * Saves via ``PUT /api/spaces/{id}/theme``; the caller page's
 * :func:`useSpaceTheme` hook picks up the new values on its next
 * mount (or an explicit refetch).
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { Button } from './Button'
import { showToast } from './Toast'

interface SpaceTheme {
  primary_color?: string | null
  accent_color?: string | null
  background_tint?: string | null
  mode_override?: string | null            // "inherit" | "light" | "dark"
  font_family?: string | null
  post_layout?: string | null              // "inherit" | "compact" | "spacious"
}

interface Preset {
  label: string
  primary: string
  accent: string
  tint: string | null
}

const PRESETS: Preset[] = [
  // "Default" matches the brand palette in tokens.css — hearth + honey.
  { label: 'Default',     primary: '#D2542A', accent: '#C8902F', tint: null },
  { label: 'Calm',        primary: '#5D7CBB', accent: '#70B3A4', tint: '#F0F4F8' },
  { label: 'Bold',        primary: '#E94E77', accent: '#FFB400', tint: '#1F1E26' },
  { label: 'Playful',     primary: '#B14AED', accent: '#FFD447', tint: '#FDF4FF' },
  { label: 'High contrast', primary: '#000000', accent: '#0050E6', tint: '#FFFFFF' },
]

const FONTS = [
  'System (default)',
  "Inter, system-ui, sans-serif",
  "'IBM Plex Sans', sans-serif",
  "'Atkinson Hyperlegible', sans-serif",
  "'JetBrains Mono', ui-monospace, monospace",
]

interface Props {
  spaceId: string
  onSaved?: () => void
}

export function SpaceThemeStudio({ spaceId, onSaved }: Props) {
  const [primary, setPrimary] = useState('#4A90E2')
  const [accent, setAccent] = useState('#F5A623')
  const [tint, setTint] = useState<string>('')          // '' means unset
  const [mode, setMode] = useState<'inherit' | 'light' | 'dark'>('inherit')
  const [font, setFont] = useState<string>(FONTS[0])
  const [layout, setLayout] = useState<'inherit' | 'compact' | 'spacious'>('inherit')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    void (async () => {
      try {
        const t = await api.get(
          `/api/spaces/${spaceId}/theme`,
        ) as SpaceTheme
        if (t.primary_color) setPrimary(t.primary_color)
        if (t.accent_color)  setAccent(t.accent_color)
        setTint(t.background_tint ?? '')
        setMode((t.mode_override as typeof mode) || 'inherit')
        setFont(t.font_family || FONTS[0])
        setLayout((t.post_layout as typeof layout) || 'inherit')
      } catch { /* keep defaults */ }
    })()
  }, [spaceId])

  const applyPreset = (p: Preset) => {
    setPrimary(p.primary)
    setAccent(p.accent)
    setTint(p.tint ?? '')
  }

  const save = async () => {
    setSaving(true)
    try {
      const body: SpaceTheme = {
        primary_color: primary,
        accent_color:  accent,
        background_tint: tint || null,
        mode_override: mode === 'inherit' ? null : mode,
        font_family:   font === FONTS[0] ? null : font,
        post_layout:   layout === 'inherit' ? null : layout,
      }
      await api.put(`/api/spaces/${spaceId}/theme`, body)
      showToast('Theme saved', 'success')
      onSaved?.()
    } catch (err: unknown) {
      showToast(
        `Save failed: ${(err as Error).message ?? err}`, 'error',
      )
    } finally {
      setSaving(false)
    }
  }

  return (
    <div class="sh-theme-studio sh-space-theme-studio">
      <h3 style={{ margin: 0 }}>Space theme</h3>
      <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)', margin: 0 }}>
        Make this space feel like yours. Changes apply only while
        members are inside this space.
      </p>

      <div class="sh-theme-presets" role="group" aria-label="Presets">
        {PRESETS.map(p => (
          <button key={p.label} type="button"
                  class="sh-theme-preset"
                  onClick={() => applyPreset(p)}
                  title={p.label}>
            <span class="sh-theme-preset-swatch"
                  style={{
                    background: `linear-gradient(135deg, ${p.primary} 0 50%, ${p.accent} 50% 100%)`,
                  }}
                  aria-hidden="true" />
            <span>{p.label}</span>
          </button>
        ))}
      </div>

      <div class="sh-theme-studio-grid">
        <label>
          Primary
          <input type="color" value={primary}
                 onInput={(e) => setPrimary((e.target as HTMLInputElement).value)} />
        </label>
        <label>
          Accent
          <input type="color" value={accent}
                 onInput={(e) => setAccent((e.target as HTMLInputElement).value)} />
        </label>
        <label>
          Background tint
          <input type="color"
                 value={tint || '#ffffff'}
                 onInput={(e) => setTint((e.target as HTMLInputElement).value)} />
          {tint && (
            <button type="button" class="sh-link" onClick={() => setTint('')}>
              Clear
            </button>
          )}
        </label>
        <label>
          Mode
          <select value={mode}
                  onChange={(e) =>
                    setMode((e.target as HTMLSelectElement).value as typeof mode)}>
            <option value="inherit">Inherit from household</option>
            <option value="light">Light</option>
            <option value="dark">Dark</option>
          </select>
        </label>
        <label>
          Font
          <select value={font}
                  onChange={(e) =>
                    setFont((e.target as HTMLSelectElement).value)}>
            {FONTS.map(f => <option key={f} value={f}>{f.split(',')[0].replace(/['"]/g, '')}</option>)}
          </select>
        </label>
        <label>
          Post layout
          <select value={layout}
                  onChange={(e) =>
                    setLayout((e.target as HTMLSelectElement).value as typeof layout)}>
            <option value="inherit">Inherit</option>
            <option value="compact">Compact</option>
            <option value="spacious">Spacious</option>
          </select>
        </label>
      </div>

      <div class="sh-theme-studio-preview"
           style={{
             '--preview-primary': primary,
             '--preview-accent': accent,
             '--preview-tint': tint || 'transparent',
           } as Record<string, string>}>
        <div class="sh-theme-preview-card">
          <div class="sh-theme-preview-header">
            <span class="sh-theme-preview-dot" /> <strong>Preview</strong>
          </div>
          <p>Posts in this space will look like this.</p>
          <div class="sh-theme-preview-chip">👍 3</div>
        </div>
      </div>

      <div class="sh-form-actions">
        <Button onClick={save} loading={saving}>Save theme</Button>
      </div>
    </div>
  )
}
