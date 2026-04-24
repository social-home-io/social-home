/**
 * PairingFlow — QR-code household pairing (§11 / §23.4).
 *
 * Two sides, one component:
 *
 *   **Show QR** (inviter): generates a QR, waits for the other side
 *   to scan, auto-fills the 6-digit SAS code when the peer's
 *   ``peer-accept`` lands, admin confirms, WS ``pairing.confirmed``
 *   flips to success.
 *
 *   **Scan QR** (scanner): camera preview via the native
 *   ``BarcodeDetector`` API, file-picker fallback for desktops,
 *   paste fallback for browsers without either. Posts the parsed
 *   payload to ``/api/pairing/accept``, shows the SAS for the scanner
 *   to read aloud to the inviter, waits for ``pairing.confirmed``.
 *
 * GFS mode: the Global-Federation-Server connect flow lives in the
 * same component to share the modal chrome + state reset.
 */
import { signal } from '@preact/signals'
import { useEffect, useRef, useState } from 'preact/hooks'
import QRCode from 'qrcode'
import { api } from '@/api'
import { ws } from '@/ws'
import { Modal } from './Modal'
import { Button } from './Button'
import { Spinner } from './Spinner'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'

type PairingMode = 'household' | 'gfs'
type PairingRole = 'unset' | 'inviter' | 'scanner'
type PairingStep =
  | 'idle'        // mode picker (inviter / scanner)
  | 'generating'  // inviter: POST /api/pairing/initiate
  | 'waiting'     // inviter: QR shown, waiting for SAS auto-fill
  | 'scanning'    // scanner: camera / upload / paste
  | 'accepting'   // scanner: POST /api/pairing/accept
  | 'sas-display' // scanner: show the 6-digit SAS for out-of-band verify
  | 'verifying'   // inviter: POST /api/pairing/confirm
  | 'success'
  | 'failed'

const step = signal<PairingStep>('idle')
const role = signal<PairingRole>('unset')
const mode = signal<PairingMode>('household')
const qrPayload = signal('')
const verificationCode = signal('')
const sasDigits = signal(['', '', '', '', '', ''])
const pairingToken = signal('')
const scannedSas = signal('')  // scanner-side SAS to display
const gfsUrl = signal('')
const open = signal(false)
const onGfsConnectedCb = signal<(() => void) | null>(null)
const peerHint = signal<string | null>(null)
const scanError = signal<string | null>(null)

export function openPairing(pairingMode: PairingMode = 'household') {
  mode.value = pairingMode
  open.value = true
  step.value = 'idle'
  role.value = 'unset'
  gfsUrl.value = ''
  peerHint.value = null
  verificationCode.value = ''
  sasDigits.value = ['', '', '', '', '', '']
  scannedSas.value = ''
  scanError.value = null
  qrPayload.value = ''
  pairingToken.value = ''
}

/**
 * Real QR renderer — encodes ``data`` to a PNG data-URL via the
 * ``qrcode`` library and displays it as an <img>. Uses error-
 * correction level M (15% redundancy) which is plenty for a
 * short URL and keeps the code visually clean.
 */
function QrCodeImg({ data, size = 240 }: { data: string; size?: number }) {
  const [src, setSrc] = useState<string | null>(null)
  useEffect(() => {
    let stopped = false
    QRCode.toDataURL(data, {
      errorCorrectionLevel: 'M',
      margin: 1,
      width: size * 2,   // 2× for retina
      color: { dark: '#0f172a', light: '#ffffff' },
    }).then(url => { if (!stopped) setSrc(url) })
      .catch(() => { /* leave src null */ })
    return () => { stopped = true }
  }, [data, size])
  if (!src) {
    return (
      <div class="sh-qr-skeleton"
           style={{ width: size, height: size }}
           aria-label="Generating QR code" />
    )
  }
  return (
    <img src={src} width={size} height={size}
         class="sh-qr-code" alt="Pairing QR code" />
  )
}

function SasInput({ autofilled }: { autofilled?: boolean }) {
  const handleDigitInput = (index: number, value: string) => {
    if (!/^\d?$/.test(value)) return
    const next = [...sasDigits.value]
    next[index] = value
    sasDigits.value = next
    verificationCode.value = next.join('')
    if (value && index < 5) {
      const nextInput = document.querySelector(
        `.sh-sas-digit[data-index="${index + 1}"]`,
      ) as HTMLInputElement | null
      nextInput?.focus()
    }
  }

  const handleKeyDown = (index: number, e: KeyboardEvent) => {
    if (e.key === 'Backspace' && !sasDigits.value[index] && index > 0) {
      const prevInput = document.querySelector(
        `.sh-sas-digit[data-index="${index - 1}"]`,
      ) as HTMLInputElement | null
      prevInput?.focus()
    }
  }

  return (
    <div class="sh-sas-input">
      <label>{t('pairing.enter_code')}</label>
      <div class={`sh-sas-digits ${autofilled ? 'sh-sas-digits--autofilled' : ''}`}>
        {sasDigits.value.map((digit, i) => (
          <input
            key={i}
            type="text"
            inputMode="numeric"
            maxLength={1}
            class="sh-sas-digit"
            data-index={i}
            value={digit}
            autoFocus={i === 0 && !autofilled}
            readOnly={autofilled}
            onInput={(e) => handleDigitInput(i, (e.target as HTMLInputElement).value)}
            onKeyDown={(e) => handleKeyDown(i, e as unknown as KeyboardEvent)}
          />
        ))}
      </div>
      {autofilled && (
        <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
          ✓ Auto-filled from the other device. Confirm to finish.
        </p>
      )}
    </div>
  )
}

/**
 * Large, readable SAS digits rendered for the scanner side. The
 * scanner reads these aloud so the inviter can compare them against
 * the auto-filled digits on their screen.
 */
function SasDisplay({ code }: { code: string }) {
  const digits = code.padStart(6, ' ').split('')
  return (
    <div class="sh-sas-display" aria-label={t('pairing.sas_display_label')}>
      {digits.map((d, i) => (
        <span key={i} class="sh-sas-display-digit">{d.trim() || '·'}</span>
      ))}
    </div>
  )
}

function GfsUrlInput({ onSubmit }: { onSubmit: () => void }) {
  return (
    <div class="sh-gfs-url-input">
      <label>{t('gfs.enter_url')}</label>
      <input
        type="url"
        class="sh-input"
        placeholder="https://gfs.example.com"
        value={gfsUrl.value}
        onInput={(e) => gfsUrl.value = (e.target as HTMLInputElement).value}
      />
      <div class="sh-pairing-actions">
        <Button onClick={onSubmit} disabled={!gfsUrl.value.trim()}>
          {t('gfs.add')}
        </Button>
      </div>
    </div>
  )
}

/**
 * Step indicator — reflects the inviter flow by default. The scanner
 * flow has its own labels since the middle step is different.
 */
function StepIndicator({ current, role: currentRole }: {
  current: PairingStep
  role: PairingRole
}) {
  const isScanner = currentRole === 'scanner'
  const labels = isScanner
    ? [
        t('pairing.step_start'),
        t('pairing.step_scan'),
        t('pairing.step_verify'),
        t('pairing.step_done'),
      ]
    : [
        t('pairing.step_start'),
        t('pairing.step_show'),
        t('pairing.step_verify'),
        t('pairing.step_done'),
      ]

  const stepIndex = (() => {
    switch (current) {
      case 'idle': return 0
      case 'generating':
      case 'waiting': return 1
      case 'scanning':
      case 'accepting': return 1
      case 'verifying':
      case 'sas-display': return 2
      default: return 3
    }
  })()

  return (
    <ol class="sh-pairing-steps" aria-label={t('pairing.progress_label')}>
      {labels.map((label, i) => (
        <li key={label}
            class={`sh-pairing-step ${i <= stepIndex ? 'sh-pairing-step--done' : ''} ${i === stepIndex ? 'sh-pairing-step--active' : ''}`}>
          <span class="sh-pairing-step-dot" aria-hidden="true">
            {i <= stepIndex ? '✓' : i + 1}
          </span>
          <span class="sh-pairing-step-label">{label}</span>
        </li>
      ))}
    </ol>
  )
}

// ────────────────────────────────────────────────────────────────
//  Scanner — camera preview / file picker / paste
// ────────────────────────────────────────────────────────────────

type BarcodeDetectorLike = {
  detect: (source: CanvasImageSource | ImageBitmap | Blob) => Promise<Array<{ rawValue: string }>>
}

function barcodeDetectorSupported(): boolean {
  return typeof (window as unknown as { BarcodeDetector?: unknown }).BarcodeDetector === 'function'
}

function createDetector(): BarcodeDetectorLike | null {
  const Ctor = (window as unknown as {
    BarcodeDetector?: new (opts: { formats: string[] }) => BarcodeDetectorLike
  }).BarcodeDetector
  if (!Ctor) return null
  try {
    return new Ctor({ formats: ['qr_code'] })
  } catch {
    return null
  }
}

/**
 * Camera preview + continuous QR decode loop.
 *
 * onPayload is called once with the decoded raw string (the JSON
 * printed in the inviter's QR). Stream + detection loop tear down
 * cleanly on unmount.
 */
function QrCameraScanner({ onPayload }: { onPayload: (raw: string) => void }) {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const [starting, setStarting] = useState(true)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    let rafId: number | null = null
    let stream: MediaStream | null = null
    const detector = createDetector()
    if (!detector) {
      setErrorMsg(t('pairing.scan_no_detector'))
      setStarting(false)
      return
    }
    (async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: { ideal: 'environment' } },
        })
        if (cancelled || !videoRef.current) {
          stream.getTracks().forEach(t => t.stop())
          return
        }
        videoRef.current.srcObject = stream
        await videoRef.current.play().catch(() => null)
        setStarting(false)
        const loop = async () => {
          if (cancelled || !videoRef.current) return
          try {
            const results = await detector.detect(videoRef.current)
            const match = results.find(r => !!r.rawValue)
            if (match) {
              cancelled = true
              onPayload(match.rawValue)
              return
            }
          } catch {
            // keep trying — detector throws on empty frames sometimes
          }
          rafId = requestAnimationFrame(() => { void loop() })
        }
        void loop()
      } catch (err: unknown) {
        const name = (err as { name?: string }).name ?? ''
        if (name === 'NotAllowedError') {
          setErrorMsg(t('pairing.scan_permission_denied'))
        } else if (name === 'NotFoundError') {
          setErrorMsg(t('pairing.scan_no_camera'))
        } else {
          setErrorMsg(t('pairing.scan_failed'))
        }
        setStarting(false)
      }
    })()
    return () => {
      cancelled = true
      if (rafId !== null) cancelAnimationFrame(rafId)
      if (stream) stream.getTracks().forEach(t => t.stop())
    }
  }, [onPayload])

  if (errorMsg) {
    return (
      <div class="sh-scan-error" role="alert">
        <div aria-hidden="true" class="sh-scan-error-icon">📷</div>
        <p>{errorMsg}</p>
      </div>
    )
  }
  return (
    <div class="sh-scan-camera">
      <video ref={videoRef} playsInline muted class="sh-scan-video" />
      <div class="sh-scan-frame" aria-hidden="true" />
      {starting && (
        <div class="sh-scan-starting">
          <Spinner />
          <span>{t('pairing.scan_starting')}</span>
        </div>
      )}
    </div>
  )
}

/**
 * Try to decode a QR from an uploaded image file. Uses
 * BarcodeDetector — for browsers without it, onPayload is never
 * called and we report a fallback-to-paste message.
 */
async function decodeImage(file: File): Promise<string | null> {
  if (!barcodeDetectorSupported()) return null
  const detector = createDetector()
  if (!detector) return null
  const bitmap = await createImageBitmap(file)
  try {
    const results = await detector.detect(bitmap)
    const match = results.find(r => !!r.rawValue)
    return match?.rawValue ?? null
  } finally {
    bitmap.close?.()
  }
}

function ScanOptions({
  onPayload,
  onPaste,
}: {
  onPayload: (raw: string) => void
  onPaste: () => void
}) {
  const [useCamera, setUseCamera] = useState(true)
  const [decoding, setDecoding] = useState(false)

  const handleFile = async (ev: Event) => {
    const input = ev.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return
    setDecoding(true)
    scanError.value = null
    try {
      const raw = await decodeImage(file)
      if (!raw) {
        scanError.value = t('pairing.scan_no_code_in_image')
        return
      }
      onPayload(raw)
    } catch {
      scanError.value = t('pairing.scan_decode_failed')
    } finally {
      setDecoding(false)
      input.value = ''
    }
  }

  return (
    <div class="sh-scan-options">
      {useCamera && barcodeDetectorSupported() && (
        <QrCameraScanner onPayload={onPayload} />
      )}
      {(!useCamera || !barcodeDetectorSupported()) && (
        <div class="sh-scan-no-camera">
          <p class="sh-muted">{t('pairing.scan_no_camera_hint')}</p>
        </div>
      )}
      {scanError.value && (
        <p class="sh-scan-error-inline" role="alert">{scanError.value}</p>
      )}
      <div class="sh-scan-fallbacks">
        {barcodeDetectorSupported() && (
          <button
            type="button"
            class="sh-link"
            onClick={() => setUseCamera(c => !c)}
          >
            {useCamera ? t('pairing.scan_hide_camera') : t('pairing.scan_show_camera')}
          </button>
        )}
        <label class="sh-link sh-scan-upload-label">
          <input
            type="file"
            accept="image/*"
            hidden
            onChange={handleFile}
            disabled={decoding}
          />
          {t('pairing.scan_upload')}
        </label>
        <button
          type="button"
          class="sh-link"
          onClick={onPaste}
        >
          {t('pairing.scan_paste')}
        </button>
      </div>
      {decoding && <Spinner />}
    </div>
  )
}

function ScanPaste({
  onSubmit,
  onCancel,
}: {
  onSubmit: (raw: string) => void
  onCancel: () => void
}) {
  const [value, setValue] = useState('')
  return (
    <div class="sh-scan-paste">
      <label>{t('pairing.scan_paste_label')}</label>
      <textarea
        class="sh-textarea"
        rows={6}
        placeholder='{"token":"...","identity_pk":"...",...}'
        value={value}
        onInput={(e) => setValue((e.target as HTMLTextAreaElement).value)}
      />
      <div class="sh-pairing-actions">
        <Button onClick={() => onSubmit(value.trim())} disabled={!value.trim()}>
          {t('pairing.scan_paste_submit')}
        </Button>
        <button type="button" class="sh-link" onClick={onCancel}>
          {t('pairing.scan_paste_cancel')}
        </button>
      </div>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────
//  Main component
// ────────────────────────────────────────────────────────────────

export function PairingFlow({ onGfsConnected }: { onGfsConnected?: () => void }) {
  onGfsConnectedCb.value = onGfsConnected ?? null
  const sasAutofilledRef = useRef(false)
  const [pasteMode, setPasteMode] = useState(false)

  // ── Live updates from the federation layer ─────────────────────────
  useEffect(() => {
    const offAccept = ws.on('pairing.accept_received', (e) => {
      const d = e.data as { token?: string; verification_code?: string }
      if (!open.value || mode.value !== 'household') return
      if (role.value !== 'inviter') return
      if (!pairingToken.value || d.token !== pairingToken.value) return
      if (!d.verification_code) return
      // Auto-fill the 6 digits — saves the user typing when the
      // other device just accepted.
      const digits = d.verification_code.split('')
      if (digits.length === 6) {
        sasDigits.value = digits
        verificationCode.value = d.verification_code
        sasAutofilledRef.current = true
      }
    })
    const offConfirm = ws.on('pairing.confirmed', (e) => {
      const d = e.data as { instance_id?: string; display_name?: string }
      if (!open.value) return
      peerHint.value = d.display_name ?? null
      step.value = 'success'
      showToast(t('pairing.successful'), 'success')
    })
    const offAborted = ws.on('pairing.aborted', (e) => {
      const d = e.data as { reason?: string }
      if (!open.value) return
      step.value = 'failed'
      if (d.reason) peerHint.value = d.reason
    })
    return () => { offAccept(); offConfirm(); offAborted() }
  }, [])

  // ── Inviter path ─────────────────────────────────────────────────
  const initiate = async () => {
    role.value = 'inviter'
    step.value = 'generating'
    peerHint.value = null
    sasAutofilledRef.current = false
    try {
      // No body: the server sources the inbox base URL from the platform
      // adapter (HA integration pushes it; standalone reads
      // [standalone].external_url). Returns 422 NOT_CONFIGURED if unset.
      const result = await api.post('/api/pairing/initiate', {}) as {
        token: string; [key: string]: unknown
      }
      qrPayload.value = JSON.stringify(result)
      pairingToken.value = result.token
      step.value = 'waiting'
    } catch (err: unknown) {
      step.value = 'failed'
      peerHint.value = (err as Error).message ?? null
    }
  }

  const verify = async () => {
    step.value = 'verifying'
    try {
      await api.post('/api/pairing/confirm', {
        token: pairingToken.value,
        verification_code: verificationCode.value,
      })
      // Success is dispatched by the WS subscriber above.
      // As a fallback, mark success after the API call resolves:
      if (step.value === 'verifying') step.value = 'success'
    } catch (err: unknown) {
      step.value = 'failed'
      peerHint.value = (err as Error).message ?? null
    }
  }

  const copyPayload = async () => {
    try {
      await navigator.clipboard.writeText(qrPayload.value)
      showToast(t('pairing.link_copied'), 'success')
    } catch {
      showToast(t('pairing.clipboard_unavailable'), 'error')
    }
  }

  // ── Scanner path ─────────────────────────────────────────────────
  const startScan = () => {
    role.value = 'scanner'
    step.value = 'scanning'
    scanError.value = null
    setPasteMode(false)
  }

  const handleScanned = async (raw: string) => {
    // Parse first — invalid JSON → show message, let user retry.
    let parsed: Record<string, unknown>
    try {
      parsed = JSON.parse(raw)
    } catch {
      scanError.value = t('pairing.scan_invalid_json')
      return
    }
    if (!parsed.token || !parsed.identity_pk || !parsed.dh_pk) {
      scanError.value = t('pairing.scan_wrong_kind')
      return
    }
    step.value = 'accepting'
    try {
      const result = await api.post('/api/pairing/accept', parsed) as {
        verification_code: string
        token: string
      }
      pairingToken.value = result.token
      scannedSas.value = result.verification_code
      step.value = 'sas-display'
    } catch (err: unknown) {
      step.value = 'failed'
      peerHint.value = (err as Error).message ?? null
    }
  }

  // ── Shared reset ─────────────────────────────────────────────────
  const resetSas = () => {
    sasDigits.value = ['', '', '', '', '', '']
    verificationCode.value = ''
    sasAutofilledRef.current = false
  }

  const resetAll = () => {
    step.value = 'idle'
    role.value = 'unset'
    resetSas()
    gfsUrl.value = ''
    peerHint.value = null
    qrPayload.value = ''
    pairingToken.value = ''
    scannedSas.value = ''
    scanError.value = null
    setPasteMode(false)
  }

  const connectGfs = async () => {
    step.value = 'generating'
    try {
      await api.post('/api/gfs/connections', { inbox_url: gfsUrl.value.trim() })
      step.value = 'success'
      showToast(t('gfs.pair_success'), 'success')
      if (onGfsConnectedCb.value) onGfsConnectedCb.value()
    } catch (err: unknown) {
      step.value = 'failed'
      peerHint.value = (err as Error).message ?? null
    }
  }

  const modalTitle = mode.value === 'gfs' ? t('gfs.title') : t('pairing.title')

  return (
    <Modal open={open.value}
           onClose={() => { open.value = false }}
           title={modalTitle}>
      <div class="sh-pairing-flow">
        {mode.value === 'household' && (
          <StepIndicator current={step.value} role={role.value} />
        )}

        {mode.value === 'household' && (
          <>
            {step.value === 'idle' && (
              <div class="sh-pairing-start">
                <div class="sh-pairing-hero" aria-hidden="true">🔗</div>
                <h3 style={{ margin: 0 }}>{t('pairing.title')}</h3>
                <p class="sh-muted">{t('pairing.intro')}</p>
                <div class="sh-pairing-role-grid">
                  <button
                    type="button"
                    class="sh-pairing-role-card"
                    onClick={initiate}
                    aria-label={t('pairing.role_show_aria')}
                  >
                    <span class="sh-pairing-role-icon" aria-hidden="true">🪪</span>
                    <span class="sh-pairing-role-title">
                      {t('pairing.role_show')}
                    </span>
                    <span class="sh-pairing-role-hint">
                      {t('pairing.role_show_hint')}
                    </span>
                  </button>
                  <button
                    type="button"
                    class="sh-pairing-role-card"
                    onClick={startScan}
                    aria-label={t('pairing.role_scan_aria')}
                  >
                    <span class="sh-pairing-role-icon" aria-hidden="true">📷</span>
                    <span class="sh-pairing-role-title">
                      {t('pairing.role_scan')}
                    </span>
                    <span class="sh-pairing-role-hint">
                      {t('pairing.role_scan_hint')}
                    </span>
                  </button>
                </div>
              </div>
            )}

            {step.value === 'generating' && <Spinner />}

            {/* Inviter — showing the QR + waiting for SAS auto-fill */}
            {step.value === 'waiting' && (
              <div class="sh-pairing-qr">
                <p class="sh-muted">{t('pairing.show_qr')}</p>
                <QrCodeImg data={qrPayload.value} size={240} />
                <div class="sh-row" style={{ gap: 'var(--sh-space-xs)', justifyContent: 'center' }}>
                  <button type="button" class="sh-link"
                          onClick={copyPayload}>
                    {t('pairing.copy_link')}
                  </button>
                </div>
                <div class="sh-pairing-waiting" role="status">
                  <span class="sh-pairing-pulse" aria-hidden="true" />
                  <span>{t('pairing.waiting')}</span>
                </div>
                <SasInput autofilled={sasAutofilledRef.current} />
                <div class="sh-pairing-actions">
                  <Button onClick={verify}
                          disabled={verificationCode.value.length !== 6}>
                    {t('pairing.verify')}
                  </Button>
                  {!sasAutofilledRef.current && (
                    <button type="button" class="sh-link" onClick={resetSas}>
                      {t('pairing.clear_code')}
                    </button>
                  )}
                </div>
              </div>
            )}

            {/* Scanner — camera / upload / paste */}
            {step.value === 'scanning' && !pasteMode && (
              <div class="sh-pairing-scan">
                <p class="sh-muted">{t('pairing.scan_intro')}</p>
                <ScanOptions
                  onPayload={handleScanned}
                  onPaste={() => setPasteMode(true)}
                />
                <div class="sh-pairing-actions">
                  <button type="button" class="sh-link" onClick={resetAll}>
                    {t('pairing.back')}
                  </button>
                </div>
              </div>
            )}

            {step.value === 'scanning' && pasteMode && (
              <ScanPaste
                onSubmit={handleScanned}
                onCancel={() => setPasteMode(false)}
              />
            )}

            {step.value === 'accepting' && (
              <div class="sh-pairing-accepting">
                <Spinner />
                <p class="sh-muted">{t('pairing.accepting')}</p>
              </div>
            )}

            {/* Scanner — SAS display */}
            {step.value === 'sas-display' && (
              <div class="sh-pairing-sas">
                <h3 style={{ margin: 0 }}>{t('pairing.sas_heading')}</h3>
                <p class="sh-muted">{t('pairing.sas_instructions')}</p>
                <SasDisplay code={scannedSas.value} />
                <div class="sh-pairing-waiting" role="status">
                  <span class="sh-pairing-pulse" aria-hidden="true" />
                  <span>{t('pairing.sas_waiting')}</span>
                </div>
                <div class="sh-pairing-actions">
                  <button type="button" class="sh-link" onClick={resetAll}>
                    {t('pairing.cancel')}
                  </button>
                </div>
              </div>
            )}

            {step.value === 'verifying' && <Spinner />}

            {step.value === 'success' && (
              <div class="sh-pairing-success">
                <div class="sh-pairing-success-burst" aria-hidden="true">
                  <span>✓</span>
                </div>
                <h3 style={{ margin: 0 }}>{t('pairing.success')}</h3>
                <p class="sh-muted">
                  {peerHint.value
                    ? t('pairing.success_named').replace('{peer}', peerHint.value)
                    : t('pairing.success_message')}
                </p>
                <Button onClick={() => { open.value = false }}>
                  {t('pairing.done')}
                </Button>
              </div>
            )}

            {step.value === 'failed' && (
              <div class="sh-pairing-failed">
                <div class="sh-pairing-fail-mark" aria-hidden="true">⚠</div>
                <h3 style={{ margin: 0 }}>{t('pairing.failed')}</h3>
                <p class="sh-muted">
                  {peerHint.value ?? t('pairing.failed_message')}
                </p>
                <Button onClick={resetAll}>{t('pairing.retry')}</Button>
              </div>
            )}
          </>
        )}

        {mode.value === 'gfs' && (
          <>
            {step.value === 'idle' && (
              <GfsUrlInput onSubmit={connectGfs} />
            )}
            {step.value === 'generating' && <Spinner />}
            {step.value === 'success' && (
              <div class="sh-pairing-success">
                <div class="sh-pairing-success-burst" aria-hidden="true">
                  <span>✓</span>
                </div>
                <h3 style={{ margin: 0 }}>{t('gfs.connected')}</h3>
                <p class="sh-muted">{t('gfs.pair_success')}</p>
                <Button onClick={() => { open.value = false }}>
                  {t('pairing.done')}
                </Button>
              </div>
            )}
            {step.value === 'failed' && (
              <div class="sh-pairing-failed">
                <div class="sh-pairing-fail-mark" aria-hidden="true">⚠</div>
                <h3 style={{ margin: 0 }}>{t('pairing.failed')}</h3>
                <p class="sh-muted">
                  {peerHint.value ?? t('gfs.pairing_failed')}
                </p>
                <Button onClick={resetAll}>{t('pairing.retry')}</Button>
              </div>
            )}
          </>
        )}
      </div>
    </Modal>
  )
}
