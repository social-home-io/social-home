/**
 * HighlightArchiveRedirect — preserves the legacy ``/highlights/archive``
 * URL by replacing it with ``/highlights?tab=archive`` on mount.
 */
import { useEffect } from 'preact/hooks'
import { useLocation } from 'preact-iso'

export default function HighlightArchiveRedirect() {
  const loc = useLocation()
  useEffect(() => {
    loc.route('/highlights?tab=archive', true)
  }, [])
  return null
}
