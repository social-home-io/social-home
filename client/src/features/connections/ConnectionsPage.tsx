/**
 * ConnectionsPage — federation connections (§23.86, §11).
 *
 * Three sections:
 *   1. Incoming auto-pair requests — admin inbox populated when one
 *      of our paired peers has introduced a new household to us.
 *      One click approves (no QR or SAS; B's vouch signature takes
 *      the place of the out-of-band verification).
 *   2. Households (HFS) with a polished paired-peer list + the
 *      outgoing "pair via a trusted peer" flow.
 *   3. Global Federation Servers.
 */
import { useEffect, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { openPairing, PairingFlow } from '@/components/PairingFlow'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { AutoPairDialog, openAutoPair } from '@/components/AutoPairDialog'
import { ConnectionDetail } from '@/components/ConnectionDetail'
import { showToast } from '@/components/Toast'
import { ws } from '@/ws'
import { currentUser } from '@/store/auth'
import { useTitle } from '@/store/pageTitle'
import type { GfsConnection } from '@/types'
import { t } from '@/i18n/i18n'
import { confirmDialog } from '@/components/confirm'
import { relativeDocsTime } from '@/utils/relativeTime'

interface Connection {
  instance_id: string
  /** The displayed name — local alias when set, else the peer's
   *  advertised display_name. The backend already resolves this. */
  display_name: string
  /** What the peer actually advertises via the federation handshake.
   *  Used by ``ConnectionDetail`` to render "They advertise themselves
   *  as <X>" alongside the editable alias input. */
  federated_display_name?: string
  local_alias?: string | null
  status: string
  paired_at?: string | null
  source?: string
  reachable: boolean
  inbox_url?: string
  intro_relay_enabled?: boolean
  unreachable_since?: string | null
  transport?: 'rtc' | 'https' | null
}

interface AutoPairRequest {
  request_id: string
  from_a_id: string
  from_a_display: string
  via_b_id: string
  via_b_display: string
  ts: string
  received_at: string
}

const connections = signal<Connection[]>([])
const gfsConnections = signal<GfsConnection[]>([])
const autoPairRequests = signal<AutoPairRequest[]>([])
const loading = signal(true)
const gfsLoading = signal(true)
const disconnectTarget = signal<GfsConnection | null>(null)

function statusDotClass(status: string): string {
  if (status === 'active' || status === 'confirmed') return 'sh-status-dot sh-status-dot--active'
  if (status === 'suspended' || status === 'unreachable') return 'sh-status-dot sh-status-dot--unreachable'
  return 'sh-status-dot sh-status-dot--pending'
}

function hfsStatusDotClass(conn: Connection): string {
  if (!conn.reachable) return 'sh-status-dot sh-status-dot--unreachable'
  if (conn.status === 'confirmed') return 'sh-status-dot sh-status-dot--active'
  return 'sh-status-dot sh-status-dot--pending'
}

function transportIcon(t: Connection['transport']) {
  if (t === 'rtc') {
    return (
      <span
        class="sh-transport-icon sh-transport-icon--rtc"
        title="Direct connection — low latency"
        aria-label="Direct (WebRTC)"
      >
        <svg width="14" height="14" viewBox="0 0 24 24"
             fill="currentColor" aria-hidden="true">
          <path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z" />
        </svg>
      </span>
    )
  }
  if (t === 'https') {
    return (
      <span
        class="sh-transport-icon sh-transport-icon--https"
        title="Via HTTPS — works, but slower than direct"
        aria-label="Via HTTPS (fallback)"
      >
        <svg width="14" height="14" viewBox="0 0 24 24"
             fill="currentColor" aria-hidden="true">
          <path d="M19 18H6a4 4 0 010-8 5 5 0 019.6-2A4 4 0 0119 18z" />
        </svg>
      </span>
    )
  }
  return null
}

async function loadConnections() {
  loading.value = true
  try {
    connections.value = await api.get('/api/connections') as Connection[]
  } catch {
    connections.value = []
  }
  loading.value = false
}

async function loadGfsConnections() {
  gfsLoading.value = true
  try {
    gfsConnections.value = await api.get('/api/gfs/connections') as GfsConnection[]
  } catch {
    gfsConnections.value = []
  }
  gfsLoading.value = false
}

async function loadAutoPairRequests() {
  try {
    autoPairRequests.value = await api.get(
      '/api/pairing/auto-pair-requests',
    ) as AutoPairRequest[]
  } catch {
    autoPairRequests.value = []
  }
}

async function approveAutoPair(r: AutoPairRequest) {
  try {
    await api.post(
      `/api/pairing/auto-pair-requests/${r.request_id}/approve`, {},
    )
    showToast(`Paired with ${r.from_a_display}`, 'success')
    autoPairRequests.value = autoPairRequests.value.filter(
      x => x.request_id !== r.request_id,
    )
  } catch (err: unknown) {
    showToast(
      `Approve failed: ${(err as Error).message ?? err}`, 'error',
    )
  }
}

async function declineAutoPair(r: AutoPairRequest) {
  if (!await confirmDialog(`Decline ${r.from_a_display}'s pairing request?`, { destructive: true })) return
  try {
    await api.post(
      `/api/pairing/auto-pair-requests/${r.request_id}/decline`,
      {},
    )
    showToast('Request declined', 'info')
    autoPairRequests.value = autoPairRequests.value.filter(
      x => x.request_id !== r.request_id,
    )
  } catch (err: unknown) {
    showToast(
      `Decline failed: ${(err as Error).message ?? err}`, 'error',
    )
  }
}

async function disconnectGfs(gfs: GfsConnection) {
  try {
    await api.delete(`/api/gfs/connections/${gfs.id}`)
    showToast(t('gfs.disconnect'), 'success')
    gfsConnections.value = gfsConnections.value.filter(c => c.id !== gfs.id)
  } catch (e: unknown) {
    showToast((e as Error).message || t('gfs.pairing_failed'), 'error')
  }
  disconnectTarget.value = null
}

async function unpair(instanceId: string) {
  if (!await confirmDialog('Unpair this household? You will lose access to its spaces.', { destructive: true })) return
  try {
    await api.delete(`/api/pairing/connections/${instanceId}`)
    showToast('Unpaired', 'info')
    await loadConnections()
  } catch (e: unknown) {
    showToast((e as Error).message || 'Unpair failed', 'error')
  }
}

export default function ConnectionsPage() {
  useTitle(t('connections.title'))
  const [autoPairBusy, setAutoPairBusy] = useState(false)
  const [detail, setDetail] = useState<Connection | null>(null)

  useEffect(() => {
    void loadConnections()
    void loadGfsConnections()
    if (currentUser.value?.is_admin) void loadAutoPairRequests()

    const off1 = ws.on('pairing.confirmed', () => {
      void loadConnections()
    })
    const off2 = ws.on('pairing.aborted', () => {
      void loadConnections()
    })
    const off3 = ws.on('pairing.auto_pair_requested', () => {
      if (currentUser.value?.is_admin) void loadAutoPairRequests()
    })
    const off4 = ws.on('peer.transport_changed', (msg) => {
      const { instance_id, transport } = msg.data as { instance_id: string; transport: 'rtc' | 'https' }
      connections.value = connections.value.map(c =>
        c.instance_id === instance_id
          ? { ...c, transport }
          : c,
      )
    })
    return () => { off1(); off2(); off3(); off4() }
  }, [])

  const confirmed = connections.value.filter(c => c.status === 'confirmed')
  const pending = connections.value.filter(c => c.status !== 'confirmed')
  const isAdmin = !!currentUser.value?.is_admin

  return (
    <div class="sh-connections">

      {/* ── Incoming auto-pair requests (admin-only inbox) ─────────── */}
      {isAdmin && autoPairRequests.value.length > 0 && (
        <section class="sh-auto-pair-inbox">
          <h2>Pair requests</h2>
          <p class="sh-muted" style={{ marginTop: 0, fontSize: 'var(--sh-font-size-sm)' }}>
            One of your trusted peers has introduced a new household.
            Approving pairs you instantly — the vouch signature
            replaces the QR scan.
          </p>
          {autoPairRequests.value.map(r => (
            <div key={r.request_id} class="sh-auto-pair-request">
              <div class="sh-auto-pair-request-body">
                <div>
                  <strong>{r.from_a_display}</strong>
                  <span class="sh-muted"
                        style={{ fontSize: 'var(--sh-font-size-sm)' }}>
                    {' '}wants to pair with you
                  </span>
                </div>
                <div class="sh-muted"
                     style={{ fontSize: 'var(--sh-font-size-xs)' }}>
                  Vouched for by <strong>{r.via_b_display}</strong>
                  {' · '}
                  <time
                    dateTime={r.received_at}
                    title={new Date(r.received_at).toLocaleString()}
                  >
                    {relativeDocsTime(r.received_at)}
                  </time>
                </div>
              </div>
              <div class="sh-row" style={{ gap: 'var(--sh-space-xs)' }}>
                <Button variant="secondary"
                        onClick={() => void declineAutoPair(r)}>
                  Decline
                </Button>
                <Button onClick={() => void approveAutoPair(r)}>
                  Approve &amp; pair
                </Button>
              </div>
            </div>
          ))}
        </section>
      )}

      {/* ── Households ─────────────────────────────────────────────── */}
      <section class="sh-connections-section">
        <div class="sh-section-header">
          <h2>{t('connections.households')}</h2>
          <div class="sh-row" style={{ gap: 'var(--sh-space-xs)' }}>
            {confirmed.length > 0 && (
              <Button variant="secondary"
                      loading={autoPairBusy}
                      onClick={() => {
                        setAutoPairBusy(false)
                        openAutoPair(confirmed.map(c => ({
                          instance_id: c.instance_id,
                          display_name: c.display_name,
                        })))
                      }}>
                Pair via a trusted peer
              </Button>
            )}
            <Button onClick={() => openPairing('household')}>
              + {t('connections.pair')}
            </Button>
          </div>
        </div>
        {loading.value ? (
          <Spinner />
        ) : connections.value.length === 0 ? (
          <div class="sh-empty-state">
            <div aria-hidden="true">🔗</div>
            <h3>{t('connections.no_connections')}</h3>
            <p class="sh-muted">{t('connections.no_connections_hint')}</p>
            <Button onClick={() => openPairing('household')}>
              {t('connections.start_pairing')}
            </Button>
          </div>
        ) : (
          <div class="sh-connection-list">
            {pending.length > 0 && (
              <div class="sh-connections-pending-hint sh-muted">
                ⏳ {pending.length} pending handshake
                {pending.length === 1 ? '' : 's'} — waiting on the other side.
              </div>
            )}
            {connections.value.map(c => (
              <div key={c.instance_id}
                   class={`sh-connection-card ${c.status === 'confirmed' ? '' : 'sh-connection-card--pending'}`}>
                <div class="sh-connection-info">
                  <span class={hfsStatusDotClass(c)} />
                  <strong>{c.display_name}</strong>
                  {transportIcon(c.transport)}
                  <span class="sh-type-badge">Household</span>
                  {c.status !== 'confirmed' && (
                    <span class="sh-muted">
                      {c.status === 'pending_sent' ? 'Waiting for scan' :
                       c.status === 'pending_received' ? 'Waiting for confirmation' :
                       c.status}
                    </span>
                  )}
                  {c.status === 'confirmed' && !c.reachable && (
                    <span class="sh-muted">Unreachable</span>
                  )}
                </div>
                <div class="sh-connection-actions">
                  {c.status === 'confirmed' && (
                    <>
                      <Button variant="secondary"
                              onClick={() => setDetail(c)}>
                        Manage
                      </Button>
                      <Button variant="danger"
                              onClick={() => void unpair(c.instance_id)}>
                        Unpair
                      </Button>
                    </>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* ── Global Federation Servers ──────────────────────────────── */}
      <section class="sh-connections-section">
        <div class="sh-section-header">
          <h2>{t('connections.global_servers')}</h2>
          <Button onClick={() => openPairing('gfs')}>+ {t('gfs.add')}</Button>
        </div>
        {gfsLoading.value ? (
          <Spinner />
        ) : gfsConnections.value.length === 0 ? (
          <div class="sh-empty-state">
            <div aria-hidden="true">🌐</div>
            <h3>{t('gfs.no_servers')}</h3>
            <p class="sh-muted">{t('gfs.no_servers_hint')}</p>
            <Button onClick={() => openPairing('gfs')}>+ {t('gfs.add')}</Button>
          </div>
        ) : (
          <div class="sh-connection-list">
            {gfsConnections.value.map(gfs => (
              <div key={gfs.id} class="sh-connection-card">
                <div class="sh-connection-info">
                  <span class={statusDotClass(gfs.status)} />
                  <strong>{gfs.display_name}</strong>
                  <span class="sh-type-badge">Global Server</span>
                  <span class="sh-muted">{gfs.inbox_url}</span>
                  <span class="sh-muted">
                    {t('gfs.published_spaces')}: {gfs.published_space_count}
                  </span>
                </div>
                <div class="sh-connection-actions">
                  <Button variant="danger"
                          onClick={() => { disconnectTarget.value = gfs }}>
                    {t('gfs.disconnect')}
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <PairingFlow onGfsConnected={loadGfsConnections} />
      <AutoPairDialog onPaired={() => void loadConnections()} />
      {detail && (
        <ConnectionDetail
          conn={{
            instance_id: detail.instance_id,
            display_name: detail.display_name,
            federated_display_name: detail.federated_display_name,
            local_alias: detail.local_alias ?? null,
            status: detail.status,
            inbox_url: detail.inbox_url ?? '',
            intro_relay_enabled: detail.intro_relay_enabled ?? true,
            unreachable_since: detail.unreachable_since ?? null,
            paired_at: detail.paired_at ?? null,
          }}
          onClose={() => setDetail(null)}
          onRevoke={() => { setDetail(null); void loadConnections() }}
          onAliasSaved={() => { setDetail(null); void loadConnections() }}
        />
      )}
      <ConfirmDialog
        open={disconnectTarget.value !== null}
        title={t('gfs.disconnect')}
        message={t('gfs.confirm_disconnect')}
        confirmLabel={t('gfs.disconnect')}
        destructive
        onConfirm={() => disconnectTarget.value && disconnectGfs(disconnectTarget.value)}
        onCancel={() => { disconnectTarget.value = null }}
      />
    </div>
  )
}
