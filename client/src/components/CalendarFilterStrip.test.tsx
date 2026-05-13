import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

// Hoisted so the ``vi.mock`` factories below can close over the same
// signals the test cases mutate. Without ``vi.hoisted`` the factories
// run before the module-level ``const`` is initialised. ``await import``
// is the lint-friendly way to load ``@preact/signals`` inside the
// hoisted block — a bare ``require`` trips ``no-require-imports``.
const { mockCurrentUser, mockHouseholdUsers } = await vi.hoisted(async () => {
  const { signal } = await import('@preact/signals')
  return {
    mockCurrentUser: signal({
      user_id: 'u-me',
      username: 'me',
      display_name: 'Me',
      is_admin: false,
      picture_url: null,
      bio: null,
      is_new_member: false,
    }),
    mockHouseholdUsers: signal(new Map([
      ['u-me', {
        user_id: 'u-me', username: 'me', display_name: 'Me',
        is_admin: false, picture_url: null, bio: null, is_new_member: false,
      }],
      ['u-pa', {
        user_id: 'u-pa', username: 'pa', display_name: 'Pascal',
        is_admin: false, picture_url: null, bio: null, is_new_member: false,
      }],
      ['u-ma', {
        user_id: 'u-ma', username: 'ma', display_name: 'Maria',
        is_admin: false, picture_url: null, bio: null, is_new_member: false,
      }],
    ])),
  }
})

vi.mock('@/store/auth', () => ({ currentUser: mockCurrentUser }))
vi.mock('@/store/householdUsers', () => ({
  householdUsers: mockHouseholdUsers,
}))

import { CalendarFilterStrip } from './CalendarFilterStrip'

const mkCal = (id: string, owner: string, name = 'Calendar') => ({
  id, owner_username: owner, name,
})

describe('CalendarFilterStrip', () => {
  beforeEach(() => {
    // Reset to default state in case a previous test mutated.
    mockCurrentUser.value = {
      ...mockCurrentUser.value,
      username: 'me',
    }
  })

  it('renders nothing when there is a single owner', () => {
    const { container } = render(
      <CalendarFilterStrip
        calendars={[mkCal('c1', 'me')]}
        visibleCalendarIds={new Set(['c1'])}
        onChange={() => {}}
        onShowAll={() => {}}
        onShowOnlyMine={() => {}}
      />,
    )
    expect(container.querySelector('.sh-cal-strip')).toBeNull()
  })

  it('renders one pin per owner and exposes both quick actions', () => {
    const { container, getByText } = render(
      <CalendarFilterStrip
        calendars={[
          mkCal('c1', 'me'),
          mkCal('c2', 'pa'),
          mkCal('c3', 'ma'),
        ]}
        visibleCalendarIds={new Set(['c1'])}
        onChange={() => {}}
        onShowAll={() => {}}
        onShowOnlyMine={() => {}}
      />,
    )
    const pins = container.querySelectorAll('.sh-cal-strip-pin')
    expect(pins.length).toBe(3)
    expect(getByText('You')).toBeTruthy()
    expect(getByText('Pascal')).toBeTruthy()
    expect(getByText('Maria')).toBeTruthy()
    expect(getByText('Just me')).toBeTruthy()
    expect(getByText('Everyone')).toBeTruthy()
  })

  it('toggling an off pin adds its owner calendars to the visible set', () => {
    const onChange = vi.fn()
    const { getByText } = render(
      <CalendarFilterStrip
        calendars={[mkCal('c1', 'me'), mkCal('c2', 'pa')]}
        visibleCalendarIds={new Set(['c1'])}
        onChange={onChange}
        onShowAll={() => {}}
        onShowOnlyMine={() => {}}
      />,
    )
    fireEvent.click(getByText('Pascal'))
    expect(onChange).toHaveBeenCalledTimes(1)
    const next = onChange.mock.calls[0][0] as Set<string>
    expect(next.has('c1')).toBe(true)
    expect(next.has('c2')).toBe(true)
  })

  it('refuses to hide the last visible owner', () => {
    const onChange = vi.fn()
    const { getByText } = render(
      <CalendarFilterStrip
        calendars={[mkCal('c1', 'me'), mkCal('c2', 'pa')]}
        visibleCalendarIds={new Set(['c1'])}
        onChange={onChange}
        onShowAll={() => {}}
        onShowOnlyMine={() => {}}
      />,
    )
    // Click own pin (the only ON pin) — should be a no-op.
    fireEvent.click(getByText('You'))
    expect(onChange).not.toHaveBeenCalled()
  })

  it('aria-label carries the state so screen readers know which pins are showing', () => {
    const { container } = render(
      <CalendarFilterStrip
        calendars={[mkCal('c1', 'me'), mkCal('c2', 'pa')]}
        visibleCalendarIds={new Set(['c1'])}
        onChange={() => {}}
        onShowAll={() => {}}
        onShowOnlyMine={() => {}}
      />,
    )
    const pins = container.querySelectorAll('.sh-cal-strip-pin')
    const labels = Array.from(pins).map(p => p.getAttribute('aria-label'))
    expect(labels.some(l => l?.startsWith('Me') && l.includes('showing'))).toBe(true)
    expect(labels.some(l => l?.startsWith('Pascal') && l.includes('hidden'))).toBe(true)
  })
})
