/**
 * CheckboxCardGroup — the multi-select sibling of {@link RadioCardGroup},
 * rendered as a vertical list of cards (icon + title + subtitle) where each
 * card is an INDEPENDENT on/off switch rather than one choice in a group.
 *
 * Each card wraps a real ``<input type="checkbox">`` (kept in the DOM, not
 * removed) so native keyboard toggling and screen-reader semantics come for
 * free; the icon, title and subtitle sit beside it. The cards reuse the
 * ``.sh-radio-card*`` classes (input-agnostic) so the look matches the space
 * cards, with the "selected" visual = checked. Controlled via per-option
 * ``checked`` flags + ``onToggle`` (no shared ``name`` — checkboxes are
 * independent, not a radio group).
 */
import type { JSX } from 'preact'

export interface CheckboxCardOption {
  value: string
  /** Emoji or short glyph shown at the leading edge of the card. */
  icon: string
  title: string
  subtitle: string
  checked: boolean
  disabled?: boolean
}

export function CheckboxCardGroup({
  legend,
  options,
  onToggle,
  disabled,
}: {
  legend: string
  options: CheckboxCardOption[]
  onToggle: (value: string) => void
  disabled?: boolean
}): JSX.Element {
  return (
    <fieldset class="sh-radio-card-group" disabled={disabled}>
      <legend class="sh-radio-card-group__legend">{legend}</legend>
      {options.map((opt) => {
        const optDisabled = disabled || opt.disabled
        return (
          <label
            key={opt.value}
            class={
              'sh-radio-card'
              + (opt.checked ? ' sh-radio-card--selected' : '')
              + (optDisabled ? ' sh-radio-card--disabled' : '')
            }
          >
            <input
              type="checkbox"
              class="sh-radio-card__input"
              value={opt.value}
              checked={opt.checked}
              disabled={optDisabled}
              onChange={() => onToggle(opt.value)}
            />
            <span class="sh-radio-card__icon" aria-hidden="true">{opt.icon}</span>
            <span class="sh-radio-card__body">
              <span class="sh-radio-card__title">{opt.title}</span>
              <span class="sh-radio-card__subtitle">{opt.subtitle}</span>
            </span>
          </label>
        )
      })}
    </fieldset>
  )
}
