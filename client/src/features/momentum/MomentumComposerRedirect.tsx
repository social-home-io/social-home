/**
 * MomentumComposerRedirect — back-compat for the deep-link routes
 * ``/momentum/new`` and ``/momentum/{id}/reply``.
 *
 * The composer used to be a routed full page. It's now a Modal mounted
 * on the host ``MomentumPage`` (see :mod:`./MomentumPage`). To keep
 * external bookmarks + push-notification deep links working, the two
 * old routes still resolve — this component opens the dialog with the
 * appropriate ``parentId`` and redirects the URL to ``/momentum`` (or
 * ``/momentum/{id}`` for replies, so dismissing the dialog leaves the
 * user on the parent moment they were replying to).
 */
import { useEffect } from 'preact/hooks'
import { useLocation, useRoute } from 'preact-iso'
import { openMomentumComposer } from '@/components/MomentumComposerDialog'

export default function MomentumComposerRedirect() {
  const { params } = useRoute()
  const loc = useLocation()
  const parentMomentId = params.momentId ?? null

  useEffect(() => {
    openMomentumComposer(parentMomentId)
    // Redirect to the host so the dialog has somewhere to mount onto.
    // Replace, not push — the deep link itself shouldn't appear in
    // history; a back-tap from the dialog should leave the user on
    // wherever they came from, not on the redirect URL.
    loc.route(parentMomentId ? `/momentum/${parentMomentId}` : '/momentum', true)
  }, [parentMomentId])

  // Render nothing while the redirect happens. The host page's spinner
  // (or content) takes over within the same tick.
  return null
}
