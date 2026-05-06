/**
 * StoryArchiveRedirect — preserves the legacy ``/stories/archive``
 * URL by replacing it with ``/stories?tab=archive`` on mount.
 */
import { useEffect } from 'preact/hooks'
import { useLocation } from 'preact-iso'

export default function StoryArchiveRedirect() {
  const loc = useLocation()
  useEffect(() => {
    loc.route('/stories?tab=archive', true)
  }, [])
  return null
}
