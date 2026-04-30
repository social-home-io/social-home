/**
 * OnboardingFlow — first-run experience (§23.1/§23.92).
 *
 * Shown when currentUser.is_new_member is true. Each step pairs a
 * short tagline with a small illustrative mock — a feed post, a
 * shopping list, a sticky note, a pairing QR — so the operator sees
 * what the surface actually feels like, not just bullet points. The
 * mocks are inert; they exist only to set expectations.
 */
import { signal } from '@preact/signals'
import { type ComponentChildren } from 'preact'
import { api } from '@/api'
import { currentUser } from '@/store/auth'
import { Button } from './Button'

const step = signal(0)

interface OnboardStep {
  title: string
  body: string
  illustration: () => ComponentChildren
}

const STEPS: OnboardStep[] = [
  {
    title: 'Welcome to Social Home',
    body: "Your private household — a feed, calendar, tasks, shopping, photos, and calls, all running on your own server and connected to Home Assistant.",
    illustration: () => (
      <div class="sh-onboard-illus">
        <div class="sh-onboard-card sh-onboard-card--welcome">
          <div class="sh-onboard-tape" aria-hidden="true" />
          <div class="sh-onboard-avatars">
            <span class="sh-onboard-avatar sh-onboard-avatar--a">M</span>
            <span class="sh-onboard-avatar sh-onboard-avatar--b">P</span>
            <span class="sh-onboard-avatar sh-onboard-avatar--c">L</span>
            <span class="sh-onboard-avatars-more">+2</span>
          </div>
          <div class="sh-onboard-card-title">The Vizeli household</div>
          <div class="sh-onboard-card-meta">5 members · paired with 3 households</div>
        </div>
      </div>
    ),
  },
  {
    title: 'A feed for the people who actually live here',
    body: 'Post photos, polls, and updates that stay inside your household. No ads, no algorithm — just the people you live with.',
    illustration: () => (
      <div class="sh-onboard-illus">
        <div class="sh-onboard-card sh-onboard-card--feed">
          <div class="sh-onboard-tape sh-onboard-tape--moss" aria-hidden="true" />
          <div class="sh-onboard-row">
            <span class="sh-onboard-avatar sh-onboard-avatar--a">M</span>
            <div>
              <div class="sh-onboard-card-title">Maria</div>
              <div class="sh-onboard-card-meta">posted in Family · 4m</div>
            </div>
          </div>
          <p class="sh-onboard-card-text">
            Pasta night again? 🍝 New recipe from grandma —
            calling it: <em>everyone’s in by 19:00.</em>
          </p>
          <div class="sh-onboard-reactions">
            <span>❤️ 3</span><span>🍝 2</span><span>💬 4</span>
          </div>
        </div>
      </div>
    ),
  },
  {
    title: 'Shared lists, calendar, and chores',
    body: "Shopping list at the door, calendar at the fridge, tasks split between everyone — all live, all visible from any phone or tablet you've signed in on.",
    illustration: () => (
      <div class="sh-onboard-illus sh-onboard-illus--pair">
        <div class="sh-onboard-card sh-onboard-card--shop">
          <div class="sh-onboard-tape" aria-hidden="true" />
          <div class="sh-onboard-card-kicker">Shopping</div>
          <ul class="sh-onboard-list">
            <li class="is-done"><span class="sh-onboard-tick" /> Sourdough</li>
            <li class="is-done"><span class="sh-onboard-tick" /> Olive oil</li>
            <li><span class="sh-onboard-tick sh-onboard-tick--empty" /> Tomatoes <em>+ Maria</em></li>
            <li><span class="sh-onboard-tick sh-onboard-tick--empty" /> Basil <em>+ Pascal</em></li>
          </ul>
        </div>
        <div class="sh-onboard-card sh-onboard-card--cal">
          <div class="sh-onboard-tape sh-onboard-tape--moss" aria-hidden="true" />
          <div class="sh-onboard-card-kicker">Tue · Jul 29</div>
          <div class="sh-onboard-card-title">Sunday brunch @ Maria's</div>
          <div class="sh-onboard-card-meta">3 households joining</div>
        </div>
      </div>
    ),
  },
  {
    title: 'Federated, end-to-end encrypted',
    body: "Connect with other households over a QR code. Every message, photo, and event is encrypted in transit — your data lives on your server.",
    illustration: () => (
      <div class="sh-onboard-illus">
        <div class="sh-onboard-card sh-onboard-card--qr">
          <div class="sh-onboard-tape sh-onboard-tape--honey" aria-hidden="true" />
          <div class="sh-onboard-card-kicker">Pair a household</div>
          <div class="sh-onboard-qr" aria-hidden="true">
            <div class="sh-onboard-qr-grid">
              {Array.from({ length: 49 }, (_, i) => (
                <span
                  key={i}
                  class={
                    [0, 6, 42, 48, 8, 12, 16, 19, 24, 28, 32, 36, 40].includes(i % 49)
                      ? 'sh-onboard-qr-cell is-on'
                      : 'sh-onboard-qr-cell'
                  }
                />
              ))}
            </div>
          </div>
          <div class="sh-onboard-card-meta">🔒 Ed25519 · expires in 5:00</div>
        </div>
      </div>
    ),
  },
]

export function OnboardingFlow({ onComplete }: { onComplete: () => void }) {
  const current = STEPS[step.value]
  const isLast = step.value === STEPS.length - 1

  // Both "Let's go" and "Skip tour" mark the wizard done. ``App.tsx``
  // gates the wizard on ``currentUser.is_new_member``, so we mirror the
  // server-side flag flip onto the local ``currentUser`` signal —
  // otherwise the App's next render re-flips ``showOnboarding`` to
  // ``true`` and the dialog reappears, making both buttons look like
  // they "do nothing".
  const finish = () => {
    api.post('/api/me/onboarding-complete').catch(() => {})
    if (currentUser.value) {
      currentUser.value = { ...currentUser.value, is_new_member: false }
    }
    onComplete()
  }

  const next = () => {
    if (isLast) {
      finish()
    } else {
      step.value++
    }
  }

  const back = () => {
    if (step.value > 0) step.value--
  }

  const skip = () => {
    finish()
  }

  return (
    <div class="sh-onboarding" role="dialog" aria-labelledby="sh-onboarding-title">
      <div class="sh-onboarding-card">
        <div class="sh-onboarding-illustration">
          {current.illustration()}
        </div>
        <h2 id="sh-onboarding-title" class="sh-onboarding-title">{current.title}</h2>
        <p class="sh-onboarding-body">{current.body}</p>
        <div
          class="sh-onboarding-dots"
          role="progressbar"
          aria-valuemin={1}
          aria-valuemax={STEPS.length}
          aria-valuenow={step.value + 1}
          aria-label={`Step ${step.value + 1} of ${STEPS.length}`}
        >
          {STEPS.map((_, i) => (
            <span
              key={i}
              class={i === step.value ? 'sh-dot sh-dot--active' : 'sh-dot'}
            />
          ))}
        </div>
        <div class="sh-onboarding-actions">
          <Button variant="secondary" onClick={skip}>Skip tour</Button>
          <div class="sh-onboarding-actions-right">
            {step.value > 0 && (
              <Button variant="secondary" onClick={back}>Back</Button>
            )}
            <Button onClick={next}>{isLast ? "Let's go" : 'Next'}</Button>
          </div>
        </div>
      </div>
    </div>
  )
}
