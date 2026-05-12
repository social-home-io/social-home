/**
 * CalendarEventDialog — event creation + detail (§23.60).
 *
 * Phase C addition: optional ``capacity`` for space events. When set,
 * the server flips members' "going" RSVPs into ``requested`` pending
 * host approval. The field is gated behind a "Limit attendance"
 * checkbox so the simple-event happy path stays uncluttered.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Modal } from './Modal'
import { Button } from './Button'
import { Avatar } from './Avatar'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import { householdUsers } from '@/store/householdUsers'
import {
  calendarInvitees,
  loadCalendarInvitees,
} from '@/store/calendarInvitees'

interface DialogCalendarSummary {
  id: string
  name: string
  owner_username: string
}

const open = signal(false)
/** The calendar the event will be written to. Defaults to the caller's
 *  own calendar; the dialog's "For:" selector lets the caller redirect
 *  the event onto another household member's calendar. */
const calendarId = signal('')
/** Full household calendar list, populated by ``openEventDialog``. The
 *  dialog renders the "For:" selector when this has 2+ entries. */
const householdCalendars = signal<DialogCalendarSummary[]>([])
const spaceId = signal<string | null>(null)
/** Set when editing an existing event — submit PATCHes instead of
 *  POSTing a new event. ``null`` for the create flow. */
const editingEventId = signal<string | null>(null)
const summary = signal('')
const startDate = signal('')
const startTime = signal('')
const endDate = signal('')
const endTime = signal('')
const allDay = signal(false)
const description = signal('')
const location = signal('')
const limitAttendance = signal(false)
const capacity = signal('')
/** Cover image (canonical ``/api/media/{filename}``). Submitted to
 *  the server. ``''`` (empty) means no cover. */
const coverUrl = signal('')
/** Browser-renderable preview URL (signed). Used only for the dialog
 *  preview ``<img>``; never sent on submit. */
const coverPreview = signal('')
const coverUploading = signal(false)
/** Cross-household invitees (paired-instance user_ids). Local
 *  household members never appear here — coordinating with a household
 *  member is done via the "For:" calendar selector. Spaces invite all
 *  members implicitly via the ``capacity`` / RSVP flow, so this stays
 *  empty for the space-event variant of the dialog. */
const attendees = signal<Set<string>>(new Set())
/** Whether to ask invitees to RSVP. Auto-derives from the attendee
 *  list (RSVP makes sense iff there's at least one cross-household
 *  invitee), but the user can still flip it off — e.g. an FYI invite
 *  that doesn't need a yes/no. */
const rsvpEnabled = signal(true)
const submitting = signal(false)

/** Open the dialog for a personal calendar.
 *
 * @param calId   The caller's own calendar id — used as the default
 *                target so the simple "create on my calendar" path
 *                works without picking anything.
 * @param available  Optional list of household calendars to surface in
 *                   the "For:" selector. When 2+ are passed the user
 *                   can redirect the event onto another member's
 *                   calendar (so e.g. Maria can put a doctor's
 *                   appointment directly on Pascal's calendar without
 *                   it showing up on hers). Pass ``[]`` to hide the
 *                   selector entirely.
 */
export function openEventDialog(
  calId: string,
  available: DialogCalendarSummary[] = [],
) {
  reset()
  calendarId.value = calId
  householdCalendars.value = available
  spaceId.value = null
  open.value = true
  void loadCalendarInvitees()
}

/** Open the dialog for a space calendar (Phase C). When ``spaceIdValue``
 *  is set, the form shows the "Limit attendance" capacity field and
 *  the submit goes to ``/api/spaces/{id}/calendar/events``. */
export function openSpaceEventDialog(spaceIdValue: string) {
  reset()
  calendarId.value = ''
  spaceId.value = spaceIdValue
  open.value = true
}

/** Shape of the event passed to :func:`openEditEventDialog`. We pull
 *  only the fields we need to pre-populate; everything else stays at
 *  the dialog's defaults. */
interface EditableEvent {
  id: string
  calendar_id: string
  summary: string
  description?: string | null
  start: string
  end: string
  all_day: boolean
  attendees?: string[]
  rsvp_enabled?: boolean
  cover_url?: string | null
  location?: string | null
}

/** Open the dialog in edit mode — pre-populate every field from
 *  ``ev`` and PATCH instead of POST on submit. The same "For:" picker
 *  appears so the user can move the event to another member's
 *  calendar at edit time. */
export function openEditEventDialog(
  ev: EditableEvent,
  available: DialogCalendarSummary[] = [],
) {
  reset()
  editingEventId.value = ev.id
  calendarId.value = ev.calendar_id
  householdCalendars.value = available
  spaceId.value = null
  summary.value = ev.summary
  description.value = ev.description ?? ''
  location.value = ev.location ?? ''
  const start = new Date(ev.start)
  const end = new Date(ev.end)
  startDate.value = start.toISOString().slice(0, 10)
  startTime.value = start.toTimeString().slice(0, 5)
  endDate.value = end.toISOString().slice(0, 10)
  endTime.value = end.toTimeString().slice(0, 5)
  allDay.value = ev.all_day
  attendees.value = new Set(ev.attendees ?? [])
  rsvpEnabled.value = !!ev.rsvp_enabled
  coverUrl.value = ev.cover_url ?? ''
  // Same value works for both submit + preview — the media endpoint
  // signs at fetch time so the browser can load the canonical URL
  // directly via ``<img src>``.
  coverPreview.value = ev.cover_url ?? ''
  open.value = true
  void loadCalendarInvitees()
}

function reset() {
  editingEventId.value = null
  summary.value = ''
  description.value = ''
  location.value = ''
  const now = new Date()
  startDate.value = now.toISOString().slice(0, 10)
  startTime.value = now.toTimeString().slice(0, 5)
  const end = new Date(now.getTime() + 3600000)
  endDate.value = end.toISOString().slice(0, 10)
  endTime.value = end.toTimeString().slice(0, 5)
  allDay.value = false
  limitAttendance.value = false
  capacity.value = ''
  attendees.value = new Set()
  rsvpEnabled.value = false
  coverUrl.value = ''
  coverPreview.value = ''
  coverUploading.value = false
}

export function CalendarEventDialog({ onCreated }: {
  /** Fired after a successful create. The caller receives the
   *  calendar id the event landed on (``null`` for space events) so
   *  the household calendar page can auto-toggle that calendar's
   *  visibility — important when the user picked "For: Pascal" and
   *  Pascal's chip wasn't already on, so the event would otherwise
   *  appear to vanish. */
  onCreated?: (calendarId: string | null) => void
}) {
  const isSpace = spaceId.value !== null

  const submit = async () => {
    if (!summary.value.trim() || submitting.value) return
    if (limitAttendance.value) {
      const cap = parseInt(capacity.value, 10)
      if (Number.isNaN(cap) || cap < 0) {
        showToast(t('event.dialog.capacity_invalid'), 'error')
        return
      }
    }
    submitting.value = true
    try {
      const start = allDay.value
        ? `${startDate.value}T00:00:00Z`
        : `${startDate.value}T${startTime.value}:00Z`
      const end = allDay.value
        ? `${endDate.value}T23:59:59Z`
        : `${endDate.value}T${endTime.value}:00Z`
      const body: Record<string, unknown> = {
        summary: summary.value,
        start,
        end,
        all_day: allDay.value,
        description: description.value || undefined,
      }
      // Cover field is tri-state on edit (omit = leave alone, null =
      // clear, string = set). Create only sends a value when the
      // user actually picked one — the server defaults to NULL.
      if (editingEventId.value) {
        body.cover_url = coverUrl.value || null
      } else if (coverUrl.value) {
        body.cover_url = coverUrl.value
      }
      // Location follows the same tri-state on edit. The trimmed
      // empty string is treated as "clear" so wiping the input clears
      // the field on the server.
      const trimmedLocation = location.value.trim()
      if (editingEventId.value) {
        body.location = trimmedLocation || null
      } else if (trimmedLocation) {
        body.location = trimmedLocation
      }
      if (limitAttendance.value && capacity.value) {
        body.capacity = parseInt(capacity.value, 10)
      }
      // Household-event invitees. Spaces broadcast to the membership
      // implicitly so this field is unused on the space variant.
      if (!isSpace) {
        // On edit we always send the attendees list (even when empty)
        // so the user can clear invitees; on create we omit the field
        // so the server's default takes effect.
        if (editingEventId.value || attendees.value.size > 0) {
          body.attendees = Array.from(attendees.value)
        }
        // RSVP is opt-in. Only meaningful when there's an attendee
        // other than the creator — otherwise nobody to ask. On edit
        // we always send the boolean so toggling off is honoured.
        if (editingEventId.value) {
          body.rsvp_enabled = rsvpEnabled.value && attendees.value.size > 0
        } else if (rsvpEnabled.value && attendees.value.size > 0) {
          body.rsvp_enabled = true
        }
      }
      if (editingEventId.value && !isSpace) {
        await api.patch(
          `/api/calendars/events/${editingEventId.value}`,
          body,
        )
        showToast('Event updated', 'success')
      } else {
        const url = isSpace
          ? `/api/spaces/${spaceId.value}/calendar/events`
          : `/api/calendars/${calendarId.value}/events`
        await api.post(url, body)
        showToast(t('event.dialog.created'), 'success')
      }
      open.value = false
      // Pass the target calendar id so the page can ensure it's
      // visible — without this, an event Maria creates for Pascal
      // doesn't appear on her view (his chip is still off) and the
      // create feels like it didn't take.
      onCreated?.(isSpace ? null : calendarId.value)
    } catch (e) {
      const msg = (e as Error)?.message || t('event.dialog.failed')
      showToast(msg, 'error')
    } finally {
      submitting.value = false
    }
  }

  return (
    <Modal open={open.value} onClose={() => (open.value = false)}
           title={editingEventId.value ? 'Edit event' : t('event.dialog.title')}>
      <div class="sh-form">
        {!isSpace && householdCalendars.value.length > 1 && (() => {
          const me = currentUser.value?.username
          // Disambiguate when the same owner has multiple calendars —
          // suffix with the calendar name. Single-calendar owners
          // stay terse ("Pascal" vs "Pascal · Work").
          const ownerCount = new Map<string, number>()
          for (const c of householdCalendars.value) {
            ownerCount.set(c.owner_username,
              (ownerCount.get(c.owner_username) ?? 0) + 1)
          }
          const selected = householdCalendars.value
            .find(c => c.id === calendarId.value)
          const targetIsMine = selected?.owner_username === me
          let targetLabel: string | null = null
          if (selected && !targetIsMine) {
            targetLabel = selected.owner_username
            for (const u of householdUsers.value.values()) {
              if (u.username === selected.owner_username) {
                targetLabel = u.display_name || u.username
                break
              }
            }
          }
          return (
            <label>
              Add to calendar
              <select
                value={calendarId.value}
                onChange={(ev) => {
                  calendarId.value = (ev.target as HTMLSelectElement).value
                }}
              >
                {householdCalendars.value.map(c => {
                  const mine = c.owner_username === me
                  let ownerLabel = c.owner_username
                  for (const u of householdUsers.value.values()) {
                    if (u.username === c.owner_username) {
                      ownerLabel = u.display_name || u.username
                      break
                    }
                  }
                  const ambiguous = (ownerCount.get(c.owner_username) ?? 1) > 1
                  const base = mine
                    ? 'My calendar'
                    : `${ownerLabel}'s calendar`
                  return (
                    <option key={c.id} value={c.id}>
                      {ambiguous ? `${base} · ${c.name}` : base}
                    </option>
                  )
                })}
              </select>
              {targetLabel ? (
                <small class="sh-form-help">
                  This event lands directly on {targetLabel}'s calendar —
                  no invite, no RSVP. Use the picker below to invite
                  someone from another household.
                </small>
              ) : (
                <small class="sh-form-help">
                  Pick another household member to drop the event on
                  their calendar instead. Cross-household friends are
                  invited via the picker below.
                </small>
              )}
            </label>
          )
        })()}
        <label>
          {t('event.dialog.summary')} *
          <input
            value={summary.value}
            onInput={(e) =>
              (summary.value = (e.target as HTMLInputElement).value)
            }
          />
        </label>
        <label>
          <input
            type="checkbox"
            checked={allDay.value}
            onChange={() => (allDay.value = !allDay.value)}
          />{' '}
          {t('event.dialog.all_day')}
        </label>
        <label>
          {t('event.dialog.start_date')}
          <input
            type="date"
            value={startDate.value}
            onInput={(e) =>
              (startDate.value = (e.target as HTMLInputElement).value)
            }
          />
        </label>
        {!allDay.value && (
          <label>
            {t('event.dialog.start_time')}
            <input
              type="time"
              value={startTime.value}
              onInput={(e) =>
                (startTime.value = (e.target as HTMLInputElement).value)
              }
            />
          </label>
        )}
        <label>
          {t('event.dialog.end_date')}
          <input
            type="date"
            value={endDate.value}
            onInput={(e) =>
              (endDate.value = (e.target as HTMLInputElement).value)
            }
          />
        </label>
        {!allDay.value && (
          <label>
            {t('event.dialog.end_time')}
            <input
              type="time"
              value={endTime.value}
              onInput={(e) =>
                (endTime.value = (e.target as HTMLInputElement).value)
              }
            />
          </label>
        )}
        <label>
          {t('event.dialog.location')}
          <input
            type="text"
            value={location.value}
            placeholder={t('event.dialog.location_placeholder')}
            onInput={(e) =>
              (location.value = (e.target as HTMLInputElement).value)
            }
            maxLength={500}
          />
        </label>
        <label>
          {t('event.dialog.description')}
          <textarea
            value={description.value}
            onInput={(e) =>
              (description.value = (e.target as HTMLTextAreaElement).value)
            }
            rows={2}
          />
        </label>

        <CoverPicker />

        {!isSpace && (() => {
          const instances = calendarInvitees.value
          const toggle = (uid: string) => {
            const next = new Set(attendees.value)
            if (next.has(uid)) next.delete(uid)
            else next.add(uid)
            attendees.value = next
          }
          return (
            <div class="sh-attendee-block">
              <span class="sh-form-label">Invite from connected households</span>
              {instances.length === 0 ? (
                <p class="sh-form-help">
                  No paired households yet. Pair a household from{' '}
                  <a href="/settings/connections">Settings → Connections</a>{' '}
                  to invite friends from another home. (Household members
                  don't need invites — pick their calendar in the "For:"
                  selector above.)
                </p>
              ) : (
                instances.map(inst => (
                  <fieldset key={inst.instance_id} class="sh-attendee-group">
                    <legend>{inst.instance_name}</legend>
                    <div class="sh-attendee-picker">
                      {inst.members.map(m => {
                        const picked = attendees.value.has(m.user_id)
                        const label = m.display_name || m.remote_username
                        return (
                          <button
                            key={m.user_id}
                            type="button"
                            class={
                              picked
                                ? 'sh-attendee-chip sh-attendee-chip--picked'
                                : 'sh-attendee-chip'
                            }
                            aria-pressed={picked}
                            onClick={() => toggle(m.user_id)}
                          >
                            <Avatar src={m.picture_url ?? null}
                                    name={label}
                                    size={20} />
                            <span>{label}</span>
                          </button>
                        )
                      })}
                    </div>
                  </fieldset>
                ))
              )}
              {attendees.value.size > 0 && (
                <label class="sh-form-row-cap" style={{ marginTop: 'var(--sh-space-xs)' }}>
                  <input
                    type="checkbox"
                    checked={rsvpEnabled.value}
                    onChange={() => (rsvpEnabled.value = !rsvpEnabled.value)}
                  />{' '}
                  Ask invitees to RSVP
                  <small class="sh-form-help" style={{ display: 'block', marginLeft: 24 }}>
                    On by default for cross-household invites. Turn off to
                    send the event without asking for a yes/no.
                  </small>
                </label>
              )}
            </div>
          )
        })()}

        {isSpace && (
          <>
            <label class="sh-form-row-cap">
              <input
                type="checkbox"
                checked={limitAttendance.value}
                onChange={() => (limitAttendance.value = !limitAttendance.value)}
              />{' '}
              {t('event.dialog.limit_attendance')}
            </label>
            {limitAttendance.value && (
              <label>
                {t('event.dialog.capacity')}
                <input
                  type="number"
                  min={0}
                  step={1}
                  value={capacity.value}
                  onInput={(e) =>
                    (capacity.value = (e.target as HTMLInputElement).value)
                  }
                />
                <small class="sh-form-help">
                  {t('event.dialog.capacity_help')}
                </small>
              </label>
            )}
          </>
        )}

        <div class="sh-form-actions">
          <Button
            variant="secondary"
            onClick={() => (open.value = false)}
          >
            {t('common.cancel')}
          </Button>
          <Button
            onClick={submit}
            loading={submitting.value}
            disabled={!summary.value.trim()}
          >
            {editingEventId.value
              ? 'Save changes'
              : t('event.dialog.create')}
          </Button>
        </div>
      </div>
    </Modal>
  )
}

/** Cover image picker — file input + live preview + remove button.
 *
 * On file pick we POST to ``/api/media/upload`` (the same endpoint
 * the post composer + gallery use), then store the canonical URL in
 * ``coverUrl`` (sent to the server) and the signed URL in
 * ``coverPreview`` (rendered inline). On "Remove cover" both
 * signals clear; the next save sends ``cover_url: null`` which the
 * route's tri-state cover handling translates to "drop the column". */
function CoverPicker() {
  const onPick = async (e: Event) => {
    const input = e.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return
    coverUploading.value = true
    try {
      const fd = new FormData()
      fd.append('file', file)
      // ``api.upload`` handles base-href + bearer auth uniformly.
      // The previous raw ``fetch('/api/media/upload', …)`` bypassed
      // ``<base href>`` and 404'd under HAOS ingress (#303).
      const data = await api.upload<{ url: string; signed_url?: string }>(
        '/api/media/upload',
        fd,
      )
      coverUrl.value = data.url
      coverPreview.value = data.signed_url || data.url
    } catch (err) {
      showToast(`Cover upload failed: ${(err as Error).message}`, 'error')
    } finally {
      coverUploading.value = false
      input.value = ''
    }
  }

  const removeCover = () => {
    coverUrl.value = ''
    coverPreview.value = ''
  }

  return (
    <div class="sh-event-cover-picker">
      <span class="sh-form-label">Cover image (optional)</span>
      {coverPreview.value ? (
        <div class="sh-event-cover-picker-preview-wrap">
          <img
            class="sh-event-cover-preview"
            src={coverPreview.value}
            alt=""
          />
          <button
            type="button"
            class="sh-event-cover-remove"
            aria-label="Remove cover"
            onClick={removeCover}
          >×</button>
        </div>
      ) : (
        <label class="sh-event-cover-picker-empty">
          <input
            type="file"
            accept="image/*"
            onChange={onPick}
            hidden
          />
          <span>
            {coverUploading.value
              ? 'Uploading…'
              : '📷 Choose cover image'}
          </span>
        </label>
      )}
    </div>
  )
}
