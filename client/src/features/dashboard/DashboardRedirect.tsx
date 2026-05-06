/**
 * DashboardRedirect — preserves the legacy ``/dashboard`` URL.
 *
 * The Corner page's canonical URL is ``/corner`` to match the
 * sidebar label; bookmarks and stored landing-path preferences
 * (``preferences.landing_path = "/dashboard"``) keep working by
 * mounting this thin component, which replaces the URL on mount.
 */
import { useEffect } from 'preact/hooks'
import { useLocation } from 'preact-iso'

export default function DashboardRedirect() {
  const loc = useLocation()
  useEffect(() => {
    loc.route('/corner', true)
  }, [])
  return null
}
