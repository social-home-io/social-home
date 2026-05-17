import { describe, it, expect, vi } from 'vitest'

vi.mock('@/api', () => ({
  api: {
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
    upload: vi.fn().mockResolvedValue({}),
  },
}))
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))
vi.mock('@/store/calendarInvitees', () => ({
  calendarInvitees: { value: [] },
  loadCalendarInvitees: vi.fn().mockResolvedValue(undefined),
}))
vi.mock('@/store/householdUsers', () => ({
  householdUsers: { value: new Map() },
  loadHouseholdUsers: vi.fn().mockResolvedValue(undefined),
}))

describe('CalendarEventDialog', () => {
  it('module exports exist', async () => {
    const mod = await import('./CalendarEventDialog')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  /** Open the dialog with a clean state and pre-seed start / end with
   *  known values so the test isn't reading "today + 1h" derived from
   *  wall-clock time. */
  async function mountDialogWithDates(
    startDate: string,
    endDate: string,
    startTime: string,
    endTime: string,
  ) {
    const { render, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./CalendarEventDialog')
    mod.openEventDialog('cal-1')
    const { container } = render(<mod.CalendarEventDialog />)
    // Locate date / time inputs by their wrapping <label> text — type
    // alone isn't enough (two date inputs, two time inputs).
    const byLabel = (prefix: string): HTMLInputElement | null => {
      const l = Array.from(container.querySelectorAll('label')).find(
        l => l.textContent?.toLowerCase().startsWith(prefix),
      )
      return l?.querySelector('input') ?? null
    }
    const startDateInput = byLabel('start date')
    const endDateInput = byLabel('end date')
    const startTimeInput = byLabel('start time')
    const endTimeInput = byLabel('end time')
    if (!startDateInput || !endDateInput) {
      throw new Error('start/end date inputs not found')
    }
    // testing-library's fireEvent simulates Preact's input event
    // properly so the controlled-component ``onInput`` runs and the
    // signal updates trigger a re-render before the next assertion.
    fireEvent.input(startDateInput, { target: { value: startDate } })
    fireEvent.input(endDateInput, { target: { value: endDate } })
    if (startTimeInput) {
      fireEvent.input(startTimeInput, { target: { value: startTime } })
    }
    if (endTimeInput) {
      fireEvent.input(endTimeInput, { target: { value: endTime } })
    }
    return {
      container,
      fireEvent,
      startDateInput,
      endDateInput,
      startTimeInput,
      endTimeInput,
    }
  }

  describe('syncEndToStart', () => {
    it('snaps end_date forward when start_date jumps past it', async () => {
      const { fireEvent, startDateInput, endDateInput }
        = await mountDialogWithDates(
          '2026-05-14', '2026-05-14', '10:00', '11:00',
        )
      // User now jumps the start date to a far-future day. The old
      // end was today + 1h, so it's now firmly in the past relative
      // to the new start — exactly the snap case.
      fireEvent.input(startDateInput, { target: { value: '2026-12-25' } })
      expect(endDateInput.value).toBe('2026-12-25')
    })

    it('leaves end_date alone when start_date stays before end_date', async () => {
      const { fireEvent, startDateInput, endDateInput }
        = await mountDialogWithDates(
          '2026-05-14', '2026-05-20', '10:00', '11:00',
        )
      // Start moves but stays before end → no snap.
      fireEvent.input(startDateInput, { target: { value: '2026-05-15' } })
      expect(endDateInput.value).toBe('2026-05-20')
    })

    it('snaps end_time forward when start_time pushes past end_time on the same day', async () => {
      const { fireEvent, startTimeInput, endTimeInput }
        = await mountDialogWithDates(
          '2026-05-14', '2026-05-14', '10:00', '11:00',
        )
      if (!startTimeInput || !endTimeInput) throw new Error('time inputs missing')
      // Bumping start to 14:00 puts the previously-valid 11:00 end
      // before the start — snap end to match.
      fireEvent.input(startTimeInput, { target: { value: '14:00' } })
      expect(endTimeInput.value).toBe('14:00')
    })

    it('does not snap end_time backwards', async () => {
      const { fireEvent, startTimeInput, endTimeInput }
        = await mountDialogWithDates(
          '2026-05-14', '2026-05-14', '10:00', '15:00',
        )
      if (!startTimeInput || !endTimeInput) throw new Error('time inputs missing')
      // Start moves but stays before end → no time snap.
      fireEvent.input(startTimeInput, { target: { value: '11:00' } })
      expect(endTimeInput.value).toBe('15:00')
    })

    it('cascades end_date AND end_time when both end fields are in the past', async () => {
      const {
        fireEvent, startDateInput, endDateInput, startTimeInput, endTimeInput,
      } = await mountDialogWithDates(
        '2026-05-14', '2026-05-14', '10:00', '11:00',
      )
      if (!startTimeInput || !endTimeInput) throw new Error('time inputs missing')
      // User picks a future start date — the helper snaps end_date.
      // Then start_time alone can still race past end_time on the
      // (now-shared) new day, so a second handler call covers that.
      fireEvent.input(startDateInput, { target: { value: '2026-06-10' } })
      fireEvent.input(startTimeInput, { target: { value: '14:00' } })
      expect(endDateInput.value).toBe('2026-06-10')
      expect(endTimeInput.value).toBe('14:00')
    })
  })
})
