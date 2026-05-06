/**
 * CallsRedirect — preserves the legacy ``/calls`` URL.
 *
 * The active-calls tray now lives as a tab inside the Chats panel
 * (``/dms?tab=calls``). Existing bookmarks and external links keep
 * working by mounting this thin component, which replaces the URL
 * on mount so back-button navigation skips the redirect.
 */
import { useEffect } from 'preact/hooks'
import { useLocation } from 'preact-iso'

export default function CallsRedirect() {
  const loc = useLocation()
  useEffect(() => {
    loc.route('/dms?tab=calls', true)
  }, [])
  return null
}
