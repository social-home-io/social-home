/**
 * Page title — shared signal driving the heading shown in the TopBar.
 *
 * Pages call :func:`useTitle` from their top-level component; the
 * TopBar renders ``pageTitle.value`` so every page shows its label
 * inline with the search bar (saves one row of vertical space vs. an
 * h1 inside the page body).
 *
 * Static pages (Calendar, Shopping, Settings, …) pass a literal
 * string. Dynamic pages (the active task list's name, a space's
 * name) re-call :func:`useTitle` whenever the underlying value
 * changes — the hook updates the signal on every render so reactive
 * computations stay live.
 */
import { signal } from '@preact/signals'
import { useEffect } from 'preact/hooks'

export const pageTitle = signal<string>('')

/** Set the TopBar title for the current page.
 *
 *  Calling without an argument (or with an empty string) clears the
 *  title — useful for pages that intentionally render their own
 *  header (e.g. SpaceFeedPage with its hero cover).
 *
 *  Cleans up on unmount so navigating away resets the signal even
 *  when the next route hasn't mounted yet.
 */
export function useTitle(title: string): void {
  useEffect(() => {
    pageTitle.value = title
    return () => {
      // Reset to empty so the topbar doesn't show a stale title from
      // the page we just left during the brief gap before the new
      // page's ``useTitle`` runs.
      pageTitle.value = ''
    }
  }, [title])
}
