/**
 * LandingDispatch — the component mounted at ``/``.
 *
 * Looks at the caller's ``landing_path`` preference:
 *   - ``/`` (default) → render :mod:`WelcomePage` — the warm
 *     "open the door" landing.
 *   - ``/feed``       → render the household feed inline.
 *   - ``/dashboard``  → redirect to :mod:`DashboardPage` via preact-iso
 *
 * The welcome page has no sidebar entry on purpose — it's only ever
 * reached by opening the app at ``/``.  Power users who prefer the
 * full corner or the raw feed can still pick those in Settings.
 */
import { useEffect } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { getLandingPath } from '@/utils/preferences'
import FeedPage from './FeedPage'
import WelcomePage from '@/features/welcome/WelcomePage'

export default function LandingDispatch() {
  const { route } = useLocation()
  const choice = getLandingPath()

  useEffect(() => {
    if (choice === '/dashboard') {
      route('/dashboard', true)   // replace so back-button doesn't bounce
    }
  }, [choice])

  if (choice === '/dashboard') return null  // about to redirect
  if (choice === '/feed')      return <FeedPage />
  return <WelcomePage />
}
