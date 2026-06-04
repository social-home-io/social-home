import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

import { CheckboxCardGroup, type CheckboxCardOption } from './CheckboxCardGroup'

const OPTS: CheckboxCardOption[] = [
  { value: 'a', icon: '🔒', title: 'Alpha', subtitle: 'first option', checked: true },
  { value: 'b', icon: '🏠', title: 'Beta', subtitle: 'second option', checked: false },
]

describe('CheckboxCardGroup', () => {
  it('renders the legend and one card per option (icon + title + subtitle)', () => {
    const { container, getByText } = render(
      <CheckboxCardGroup legend="Pick any" options={OPTS} onToggle={() => {}} />,
    )
    expect(getByText('Pick any')).toBeTruthy()
    expect(getByText('Alpha')).toBeTruthy()
    expect(getByText('first option')).toBeTruthy()
    expect(getByText('🔒')).toBeTruthy()
    expect(getByText('Beta')).toBeTruthy()
    expect(getByText('second option')).toBeTruthy()
    const boxes = container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')
    expect(boxes).toHaveLength(2)
  })

  it('marks a checked option (checked input + selected class)', () => {
    const { container } = render(
      <CheckboxCardGroup legend="L" options={OPTS} onToggle={() => {}} />,
    )
    const cards = container.querySelectorAll('.sh-radio-card')
    expect(cards[0].classList.contains('sh-radio-card--selected')).toBe(true)
    expect(cards[1].classList.contains('sh-radio-card--selected')).toBe(false)
    const boxes = container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')
    expect(boxes[0].checked).toBe(true)
    expect(boxes[1].checked).toBe(false)
  })

  it('calls onToggle with the option value when a card input is clicked', () => {
    const onToggle = vi.fn()
    const { container } = render(
      <CheckboxCardGroup legend="L" options={OPTS} onToggle={onToggle} />,
    )
    const boxes = container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')
    fireEvent.click(boxes[1])
    expect(onToggle).toHaveBeenCalledWith('b')
  })

  it('disables an individual option flagged disabled (input + disabled class)', () => {
    const opts: CheckboxCardOption[] = [
      { value: 'a', icon: '', title: 'A', subtitle: '', checked: false },
      { value: 'b', icon: '', title: 'B', subtitle: '', checked: false, disabled: true },
    ]
    const { container } = render(
      <CheckboxCardGroup legend="L" options={opts} onToggle={() => {}} />,
    )
    const boxes = container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')
    expect(boxes[0].disabled).toBe(false)
    expect(boxes[1].disabled).toBe(true)
    expect(container.querySelectorAll('.sh-radio-card--disabled')).toHaveLength(1)
  })
})
