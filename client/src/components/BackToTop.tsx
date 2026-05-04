/**
 * BackToTop — floating button that appears once the user has scrolled
 * past the fold and snaps the viewport back to the top on click.
 *
 * Used on long lists (feed, DM thread, gallery) where reaching the top
 * to refresh / start a new post is a slog. Mounted once at the App
 * level and listens to ``window.scroll``; it's invisible above the
 * threshold so resting state doesn't add chrome.
 *
 * The button is a single fixed-position circle in the bottom-right.
 * Honours ``prefers-reduced-motion``: smooth scroll on the default
 * preference, instant snap when the user has reduced motion turned on.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'

const SHOW_AFTER_PX = 600

const visible = signal(false)

export function BackToTop() {
  useEffect(() => {
    const onScroll = () => {
      visible.value = window.scrollY > SHOW_AFTER_PX
    }
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  if (!visible.value) return null

  const onClick = () => {
    const reduced = typeof window !== 'undefined'
      && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    window.scrollTo({ top: 0, behavior: reduced ? 'auto' : 'smooth' })
  }

  return (
    <button
      type="button"
      class="sh-back-to-top"
      onClick={onClick}
      aria-label="Back to top"
      title="Back to top"
    >
      ↑
    </button>
  )
}
