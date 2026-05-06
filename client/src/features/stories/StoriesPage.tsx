/**
 * StoriesPage — host for the personal Stories pillar (§Stories).
 *
 * Two tabs: ``inbox`` (recent rings + per-author list) and
 * ``archive`` (month-grid retention-window browser). Tab state is
 * URL-driven via the ``?tab=`` query param so deep links from
 * elsewhere in the app and external bookmarks (``/stories/archive``
 * redirects to ``/stories?tab=archive``) land on the right tab.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { TabHeader } from '@/components/TabHeader'
import { useTitle } from '@/store/pageTitle'
import StoriesInboxTab from './StoriesInboxTab'
import StoryArchiveTab from './StoryArchiveTab'

type StoriesTab = 'inbox' | 'archive'

const TABS: readonly StoriesTab[] = ['inbox', 'archive'] as const
const TAB_LABELS: Readonly<Record<StoriesTab, string>> = {
  inbox:   'Stories',
  archive: 'Archive',
}

const activeTab = signal<StoriesTab>('inbox')


function tabFromUrl(url: string): StoriesTab {
  const q = url.split('?')[1] ?? ''
  const t = new URLSearchParams(q).get('tab')
  return t === 'archive' ? 'archive' : 'inbox'
}


export default function StoriesPage() {
  useTitle(activeTab.value === 'archive' ? 'Story archive' : 'Stories')
  const loc = useLocation()

  useEffect(() => {
    activeTab.value = tabFromUrl(loc.url)
  }, [loc.url])

  const onSelectTab = (tab: StoriesTab) => {
    activeTab.value = tab
    // Preserve any other query params (e.g. ``?tag=`` if a future
    // filter lands on Stories) by re-emitting them alongside ``tab``.
    const [path, query] = loc.url.split('?')
    const params = new URLSearchParams(query ?? '')
    if (tab === 'inbox') params.delete('tab')
    else params.set('tab', tab)
    const next = params.toString()
      ? `${path.split('?')[0] || '/stories'}?${params.toString()}`
      : '/stories'
    if (loc.url !== next) loc.route(next, true)
  }

  return (
    <div class="sh-stories">
      <TabHeader<StoriesTab>
        activeTab={activeTab.value}
        visibleTabs={TABS}
        labels={TAB_LABELS}
        ariaLabel="Stories sections"
        onSelectTab={onSelectTab}
        actions={
          <a href="/settings#stories" class="sh-link">Settings</a>
        }
      />
      {activeTab.value === 'archive' ? <StoryArchiveTab /> : <StoriesInboxTab />}
    </div>
  )
}
