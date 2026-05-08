/**
 * CpAdminPanel — Child Protection admin panel (spec §23.103).
 *
 * Admin-only surface that lets the household set protection on / off per
 * user, supply a ``declared_age`` + optional DOB, and manage the list of
 * guardians for each protected minor.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import { Button } from '@/components/Button'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import type { User } from '@/types'

interface MinorFormState {
  username: string
  declared_age: number
  date_of_birth: string
}

const users = signal<User[]>([])
const loading = signal(true)
/** For the active row: pending enable form (username → state). */
const pendingEnable = signal<MinorFormState | null>(null)
const pendingDisable = signal<string | null>(null)
/** Guardian admin state for the currently expanded minor. */
const guardiansFor = signal<string | null>(null)
const guardians = signal<string[]>([])

async function load() {
  loading.value = true
  try {
    users.value = await api.get('/api/users') as User[]
  } catch (e: unknown) {
    showToast((e as Error).message || 'Failed to load users', 'error')
  } finally {
    loading.value = false
  }
}

async function loadGuardians(minorUserId: string) {
  try {
    const data = await api.get(`/api/cp/users/${minorUserId}/guardians`) as {
      guardians: string[]
    }
    guardians.value = data.guardians
  } catch {
    guardians.value = []
  }
}

async function enableProtection(form: MinorFormState) {
  try {
    const body: Record<string, unknown> = {
      enabled: true, declared_age: form.declared_age,
    }
    if (form.date_of_birth) body.date_of_birth = form.date_of_birth
    await api.post(
      `/api/cp/users/${form.username}/protection`, body,
    )
    showToast('Protection enabled', 'success')
    pendingEnable.value = null
    void load()
  } catch (e: unknown) {
    showToast((e as Error).message || 'Enable failed', 'error')
  }
}

async function disableProtection(username: string) {
  try {
    await api.post(
      `/api/cp/users/${username}/protection`, { enabled: false },
    )
    showToast('Protection removed', 'info')
    void load()
  } catch (e: unknown) {
    showToast((e as Error).message || 'Disable failed', 'error')
  }
  pendingDisable.value = null
}

async function addGuardian(minorUserId: string, guardianUserId: string) {
  try {
    await api.post(
      `/api/cp/users/${minorUserId}/guardians/${guardianUserId}`, {},
    )
    await loadGuardians(minorUserId)
  } catch (e: unknown) {
    showToast((e as Error).message || 'Add guardian failed', 'error')
  }
}

async function removeGuardian(minorUserId: string, guardianUserId: string) {
  try {
    await api.delete(`/api/cp/users/${minorUserId}/guardians/${guardianUserId}`)
    await loadGuardians(minorUserId)
  } catch (e: unknown) {
    showToast((e as Error).message || 'Remove guardian failed', 'error')
  }
}

export default function CpAdminPanel() {
  useEffect(() => {
    void load()
    // Reload when another admin toggles protection elsewhere.
    const off = ws.on('cp.protection_enabled', () => { void load() })
    const off2 = ws.on('cp.protection_disabled', () => { void load() })
    return () => { off(); off2() }
  }, [])

  if (loading.value) return <Spinner />

  return (
    <section class="sh-cp-admin sh-admin-section">
      <h2>Child protection</h2>
      <p class="sh-muted">
        Mark household members as protected minors, assign guardians, and
        set minimum ages on spaces. Minors are blocked from joining spaces
        whose <code>min_age</code> exceeds their declared age, and their
        DMs are restricted to directly-paired instances.
      </p>

      <table class="sh-admin-table">
        <thead>
          <tr><th>Name</th><th>Username</th><th>Protected</th><th>Actions</th></tr>
        </thead>
        <tbody>
          {users.value.map(u => {
            const protectedUser = Boolean((u as User & { is_minor?: boolean }).is_minor)
            return (
              <>
                <tr key={u.username}>
                  <td>{u.display_name}</td>
                  <td>@{u.username}</td>
                  <td>{protectedUser ? '🔒 Yes' : '—'}</td>
                  <td>
                    {protectedUser ? (
                      <>
                        <Button variant="secondary"
                                onClick={() => {
                                  guardiansFor.value =
                                    guardiansFor.value === u.user_id ? null : u.user_id
                                  if (guardiansFor.value) void loadGuardians(u.user_id)
                                }}>
                          Guardians
                        </Button>
                        <Button variant="secondary"
                                onClick={() => pendingDisable.value = u.username}>
                          Remove protection
                        </Button>
                      </>
                    ) : (
                      <Button
                        variant="secondary"
                        onClick={() => pendingEnable.value = {
                          username: u.username,
                          declared_age: 12,
                          date_of_birth: '',
                        }}>
                        Mark as minor
                      </Button>
                    )}
                  </td>
                </tr>
                {guardiansFor.value === u.user_id && (
                  <tr><td colspan={4} class="sh-cp-guardians-row">
                    <GuardianList minorUserId={u.user_id} allUsers={users.value} />
                  </td></tr>
                )}
              </>
            )
          })}
        </tbody>
      </table>

      {pendingEnable.value && (
        <EnableForm
          state={pendingEnable.value}
          onChange={(s) => pendingEnable.value = s}
          onSubmit={() => enableProtection(pendingEnable.value!)}
          onCancel={() => pendingEnable.value = null}
        />
      )}

      <ConfirmDialog
        open={pendingDisable.value !== null}
        title="Remove child protection?"
        message="This clears minor status and lifts DM + space restrictions."
        onConfirm={() => pendingDisable.value && disableProtection(pendingDisable.value)}
        onCancel={() => pendingDisable.value = null}
      />
    </section>
  )
}

function EnableForm({ state, onChange, onSubmit, onCancel }: {
  state: MinorFormState
  onChange: (s: MinorFormState) => void
  onSubmit: () => void
  onCancel: () => void
}) {
  return (
    <div class="sh-cp-enable-form sh-card">
      <h3>Enable protection for @{state.username}</h3>
      <label>
        Declared age (0–17)
        <input type="number" min={0} max={17}
          value={state.declared_age}
          onInput={(e) => onChange({
            ...state,
            declared_age: Math.max(0, Math.min(17, Number(
              (e.target as HTMLInputElement).value,
            ) || 0)),
          })} />
      </label>
      <label>
        Date of birth (optional)
        <input type="date"
          value={state.date_of_birth}
          onInput={(e) => onChange({
            ...state,
            date_of_birth: (e.target as HTMLInputElement).value,
          })} />
      </label>
      <div class="sh-row">
        <Button onClick={onSubmit}>Enable</Button>
        <Button variant="secondary" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  )
}

function GuardianList({ minorUserId, allUsers }: {
  minorUserId: string
  allUsers: User[]
}) {
  const candidates = allUsers.filter(u => u.user_id !== minorUserId)
  const picker = signal<string>('')

  return (
    <div class="sh-cp-guardian-list">
      <strong>Guardians</strong>
      <ul>
        {guardians.value.length === 0 && <li class="sh-muted">No guardians assigned</li>}
        {guardians.value.map(gid => {
          const u = allUsers.find(x => x.user_id === gid)
          return (
            <li key={gid} class="sh-row">
              <span>{u ? u.display_name : gid}</span>
              <Button variant="secondary"
                      onClick={() => removeGuardian(minorUserId, gid)}>
                Remove
              </Button>
            </li>
          )
        })}
      </ul>
      <div class="sh-row">
        <select value={picker.value}
                onChange={(e) => picker.value = (e.target as HTMLSelectElement).value}>
          <option value="">— pick a guardian —</option>
          {candidates.map(u => (
            <option key={u.user_id} value={u.user_id}>
              {u.display_name} (@{u.username})
            </option>
          ))}
        </select>
        <Button onClick={() => {
          if (picker.value) {
            void addGuardian(minorUserId, picker.value)
            picker.value = ''
          }
        }}>Add</Button>
      </div>
    </div>
  )
}
