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

  it('sends announce_in_feed when announcing a space event (§23.15)', async () => {
    const { render, fireEvent } = await import('@testing-library/preact')
    const { api } = await import('@/api')
    ;(api.post as ReturnType<typeof vi.fn>).mockClear()
    const mod = await import('./CalendarEventDialog')
    mod.openSpaceEventDialog('sp-1')
    const { container } = render(<mod.CalendarEventDialog />)
    // Summary is the only untyped text input; dates/times carry a type.
    const summary = container.querySelector('input:not([type])') as HTMLInputElement
    fireEvent.input(summary, { target: { value: 'Picnic' } })
    // Default: the announce checkbox is present and OFF.
    const announceLabel = Array.from(container.querySelectorAll('label')).find(
      (l) => /announce this event in the space feed/i.test(l.textContent ?? ''),
    )
    expect(announceLabel).toBeTruthy()
    const cb = announceLabel!.querySelector('input[type="checkbox"]') as HTMLInputElement
    expect(cb.checked).toBe(false)
    fireEvent.click(cb)
    const buttons = Array.from(container.querySelectorAll('button'))
    fireEvent.click(buttons[buttons.length - 1])  // Create
    await new Promise((r) => setTimeout(r, 0))
    const call = (api.post as ReturnType<typeof vi.fn>).mock.calls.find(
      (c: unknown[]) => String(c[0]).includes('/calendar/events'),
    )
    expect(call).toBeTruthy()
    expect((call![1] as { announce_in_feed?: boolean }).announce_in_feed).toBe(true)
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

  describe('edit full-sync', () => {
    const CALS = [
      { id: 'calA', name: 'A cal', owner_username: 'admin', color: null },
      { id: 'calB', name: 'B cal', owner_username: 'bob', color: null },
    ]

    /** Find a calendar-target chip by its visible headline (the
     *  CreateCalendarPicker renders "You" for the caller and the owner
     *  display name — here the bare username — for the others). */
    function chipByName(
      container: Element,
      label: string,
    ): HTMLButtonElement | undefined {
      return Array.from(
        container.querySelectorAll('button.sh-cal-target-chip'),
      ).find(b => (b.textContent ?? '').includes(label)) as
        | HTMLButtonElement
        | undefined
    }

    function summaryInput(container: Element): HTMLInputElement {
      return container.querySelector('input:not([type])') as HTMLInputElement
    }

    function saveButton(container: Element): HTMLButtonElement {
      const buttons = Array.from(container.querySelectorAll('button'))
      return buttons[buttons.length - 1] as HTMLButtonElement
    }

    it('pre-checks every member already in the group', async () => {
      const { render } = await import('@testing-library/preact')
      const mod = await import('./CalendarEventDialog')
      mod.openEditEventDialog(
        {
          id: 'ev1',
          calendar_id: 'calA',
          summary: 'Dinner',
          start: '2026-06-10T18:00:00+00:00',
          end: '2026-06-10T19:00:00+00:00',
          all_day: false,
          client_event_uuid: 'uuid-1',
          grouped_calendar_ids: ['calA', 'calB'],
          grouped_event_ids: ['ev1', 'ev2'],
        },
        CALS,
      )
      const { container } = render(<mod.CalendarEventDialog />)
      const chipA = chipByName(container, 'You')
      const chipB = chipByName(container, 'bob')
      expect(chipA?.getAttribute('aria-pressed')).toBe('true')
      expect(chipB?.getAttribute('aria-pressed')).toBe('true')
    })

    it('adds a new member: PATCHes the existing copy and POSTs the new one, same uuid', async () => {
      const { render, fireEvent } = await import('@testing-library/preact')
      const { api } = await import('@/api')
      ;(api.patch as ReturnType<typeof vi.fn>).mockClear()
      ;(api.post as ReturnType<typeof vi.fn>).mockClear()
      ;(api.delete as ReturnType<typeof vi.fn>).mockClear()
      const mod = await import('./CalendarEventDialog')
      mod.openEditEventDialog(
        {
          id: 'ev1',
          calendar_id: 'calA',
          summary: 'Dinner',
          start: '2026-06-10T18:00:00+00:00',
          end: '2026-06-10T19:00:00+00:00',
          all_day: false,
          client_event_uuid: 'uuid-1',
          grouped_calendar_ids: ['calA'],
          grouped_event_ids: ['ev1'],
        },
        CALS,
      )
      const { container } = render(<mod.CalendarEventDialog />)
      fireEvent.click(chipByName(container, 'bob')!)
      fireEvent.click(saveButton(container))
      await new Promise(r => setTimeout(r, 0))
      const patchCall = (api.patch as ReturnType<typeof vi.fn>).mock.calls.find(
        (c: unknown[]) => String(c[0]).endsWith('/api/calendars/events/ev1'),
      )
      const postCall = (api.post as ReturnType<typeof vi.fn>).mock.calls.find(
        (c: unknown[]) => String(c[0]) === '/api/calendars/calB/events',
      )
      expect(patchCall).toBeTruthy()
      expect(postCall).toBeTruthy()
      expect((patchCall![1] as { client_event_uuid?: string }).client_event_uuid)
        .toBe('uuid-1')
      expect((postCall![1] as { client_event_uuid?: string }).client_event_uuid)
        .toBe('uuid-1')
    })

    it('removes an unticked member: PATCHes survivor and DELETEs the dropped copy', async () => {
      const { render, fireEvent } = await import('@testing-library/preact')
      const { api } = await import('@/api')
      ;(api.patch as ReturnType<typeof vi.fn>).mockClear()
      ;(api.delete as ReturnType<typeof vi.fn>).mockClear()
      const mod = await import('./CalendarEventDialog')
      mod.openEditEventDialog(
        {
          id: 'ev1',
          calendar_id: 'calA',
          summary: 'Dinner',
          start: '2026-06-10T18:00:00+00:00',
          end: '2026-06-10T19:00:00+00:00',
          all_day: false,
          client_event_uuid: 'uuid-1',
          grouped_calendar_ids: ['calA', 'calB'],
          grouped_event_ids: ['ev1', 'ev2'],
        },
        CALS,
      )
      const { container } = render(<mod.CalendarEventDialog />)
      // Untick bob (calB → ev2).
      fireEvent.click(chipByName(container, 'bob')!)
      fireEvent.click(saveButton(container))
      await new Promise(r => setTimeout(r, 0))
      const patchCall = (api.patch as ReturnType<typeof vi.fn>).mock.calls.find(
        (c: unknown[]) => String(c[0]).endsWith('/api/calendars/events/ev1'),
      )
      const deleteCall = (api.delete as ReturnType<typeof vi.fn>).mock.calls.find(
        (c: unknown[]) => String(c[0]).endsWith('/api/calendars/events/ev2'),
      )
      expect(patchCall).toBeTruthy()
      expect(deleteCall).toBeTruthy()
    })

    it('propagates a content change to every still-ticked copy', async () => {
      const { render, fireEvent } = await import('@testing-library/preact')
      const { api } = await import('@/api')
      ;(api.patch as ReturnType<typeof vi.fn>).mockClear()
      const mod = await import('./CalendarEventDialog')
      mod.openEditEventDialog(
        {
          id: 'ev1',
          calendar_id: 'calA',
          summary: 'Dinner',
          start: '2026-06-10T18:00:00+00:00',
          end: '2026-06-10T19:00:00+00:00',
          all_day: false,
          client_event_uuid: 'uuid-1',
          grouped_calendar_ids: ['calA', 'calB'],
          grouped_event_ids: ['ev1', 'ev2'],
        },
        CALS,
      )
      const { container } = render(<mod.CalendarEventDialog />)
      fireEvent.input(summaryInput(container), { target: { value: 'Brunch' } })
      fireEvent.click(saveButton(container))
      await new Promise(r => setTimeout(r, 0))
      const calls = (api.patch as ReturnType<typeof vi.fn>).mock.calls
      const ev1 = calls.find(
        (c: unknown[]) => String(c[0]).endsWith('/api/calendars/events/ev1'),
      )
      const ev2 = calls.find(
        (c: unknown[]) => String(c[0]).endsWith('/api/calendars/events/ev2'),
      )
      expect((ev1![1] as { summary?: string }).summary).toBe('Brunch')
      expect((ev2![1] as { summary?: string }).summary).toBe('Brunch')
    })

    it('promotes a legacy single event: mints a uuid carried on PATCH + the new POST', async () => {
      const { render, fireEvent } = await import('@testing-library/preact')
      const { api } = await import('@/api')
      ;(api.patch as ReturnType<typeof vi.fn>).mockClear()
      ;(api.post as ReturnType<typeof vi.fn>).mockClear()
      const mod = await import('./CalendarEventDialog')
      mod.openEditEventDialog(
        {
          id: 'ev1',
          calendar_id: 'calA',
          summary: 'Dinner',
          start: '2026-06-10T18:00:00+00:00',
          end: '2026-06-10T19:00:00+00:00',
          all_day: false,
          // No client_event_uuid, no grouped arrays — legacy single.
        },
        CALS,
      )
      const { container } = render(<mod.CalendarEventDialog />)
      fireEvent.click(chipByName(container, 'bob')!)
      fireEvent.click(saveButton(container))
      await new Promise(r => setTimeout(r, 0))
      const patchCall = (api.patch as ReturnType<typeof vi.fn>).mock.calls.find(
        (c: unknown[]) => String(c[0]).endsWith('/api/calendars/events/ev1'),
      )
      const postCall = (api.post as ReturnType<typeof vi.fn>).mock.calls.find(
        (c: unknown[]) => String(c[0]) === '/api/calendars/calB/events',
      )
      const patchedUuid = (patchCall![1] as { client_event_uuid?: string })
        .client_event_uuid
      const postedUuid = (postCall![1] as { client_event_uuid?: string })
        .client_event_uuid
      expect(patchedUuid).toBeTruthy()
      expect(postedUuid).toBe(patchedUuid)
    })
  })
})
