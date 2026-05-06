/**
 * MomentumArchiveRedirect — preserves the legacy ``/momentum/archive``
 * URL by replacing it with ``/momentum?tab=archive`` on mount,
 * carrying any ``?tag=…`` filter forward so existing hashtag deep
 * links keep working.
 */
import { useEffect } from 'preact/hooks'
import { useLocation } from 'preact-iso'

export default function MomentumArchiveRedirect() {
  const loc = useLocation()
  useEffect(() => {
    const q = loc.url.split('?')[1] ?? ''
    const params = new URLSearchParams(q)
    const tag = params.get('tag')
    const target = tag
      ? `/momentum?tab=archive&tag=${encodeURIComponent(tag)}`
      : '/momentum?tab=archive'
    loc.route(target, true)
  }, [])
  return null
}
