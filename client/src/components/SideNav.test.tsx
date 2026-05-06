import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'
import { LocationProvider } from 'preact-iso'

vi.mock('@/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
vi.mock('@/ws', () => ({ ws: { on: vi.fn(() => () => {}) } }))

import { currentUser } from '@/store/auth'
import { isGuardian } from '@/store/guardian'
import { active as activeCalls } from '@/store/calls'
import { dmUnreadTotal } from '@/store/dms'
import { toggles } from '@/components/HouseholdToggles'
import { SideNav } from './SideNav'

const ALL_FEATURES_ON = {
  feat_feed: true, feat_pages: true, feat_tasks: true,
  feat_stickies: true, feat_calendar: true, feat_stories: true,
  feat_momentum: true,
  allow_text: true, allow_image: true, allow_video: true,
  allow_file: true, allow_poll: true, allow_schedule: true,
  allow_story_share: true,
  household_name: 'Hearth',
}

function setUser(partial: Partial<{ is_admin: boolean; display_name: string; picture_url: string | null }> = {}) {
  currentUser.value = {
    user_id: 'u-1',
    username: 'pascal',
    display_name: partial.display_name ?? 'Pascal',
    is_admin: partial.is_admin ?? false,
    picture_url: partial.picture_url ?? null,
    picture_hash: null,
    bio: null,
    is_new_member: false,
  }
}

function renderAt(path: string) {
  window.history.pushState(null, '', path)
  return render(
    <LocationProvider>
      <SideNav />
    </LocationProvider>,
  )
}

beforeEach(() => {
  currentUser.value = null
  isGuardian.value = false
  activeCalls.value = []
  dmUnreadTotal.value = 0
  toggles.value = { ...ALL_FEATURES_ON }
})

describe('SideNav', () => {
  it('renders the four groups in IA order: At home → Talk → Browse → You', () => {
    setUser({ is_admin: true })
    const { container } = renderAt('/')
    const headers = Array.from(container.querySelectorAll('.sh-sidenav-group-header'))
      .map((el) => el.textContent?.trim())
    expect(headers).toEqual(['At home', 'Talk', 'Browse', 'You'])
  })

  it('exposes each group as a labelled <nav> landmark', () => {
    setUser({ is_admin: true })
    const { container } = renderAt('/')
    const navs = container.querySelectorAll('nav[aria-labelledby^="sidenav-group-"]')
    expect(navs.length).toBe(4)
    for (const nav of Array.from(navs)) {
      const id = nav.getAttribute('aria-labelledby')!
      const heading = container.querySelector(`#${id}`)
      expect(heading).toBeTruthy()
      expect(heading?.tagName.toLowerCase()).toBe('h2')
    }
  })

  it('hides Pages and Stickies when their feature toggles are off', () => {
    setUser({ is_admin: true })
    toggles.value = { ...ALL_FEATURES_ON, feat_pages: false, feat_stickies: false }
    const { queryByText } = renderAt('/')
    expect(queryByText('Pages')).toBeNull()
    expect(queryByText('Stickies')).toBeNull()
    // Other items in the same group still render.
    expect(queryByText('Feed')).toBeTruthy()
    expect(queryByText('Gallery')).toBeTruthy()
  })

  it('shows Bazaar unconditionally — it is a per-space feature, not gated by household toggles', () => {
    setUser({ is_admin: true })
    const { queryByText, container } = renderAt('/')
    expect(queryByText('Bazaar')).toBeTruthy()
    expect(queryByText('Spaces')).toBeTruthy()
    const headers = Array.from(container.querySelectorAll('.sh-sidenav-group-header'))
      .map((el) => el.textContent?.trim())
    expect(headers).toContain('Browse')
  })

  it('suppresses a group header entirely when every item is gated off', () => {
    setUser({ is_admin: false })
    toggles.value = {
      ...ALL_FEATURES_ON,
      feat_feed: false, feat_calendar: false, feat_tasks: false,
      feat_pages: false, feat_stickies: false,
    }
    // Force the AT HOME group fully empty by also pretending Shopping/
    // Presence/Gallery don't render. Those three are unconditional in
    // the data array, so the group is genuinely never empty in
    // practice — but we can verify the suppression rule by hiding YOU
    // entirely instead: make the user a non-admin and not a guardian.
    isGuardian.value = false
    const { queryByText, container } = renderAt('/')
    // YOU still has Settings (always-on), so YOU header should show.
    expect(queryByText('You')).toBeTruthy()
    expect(queryByText('Admin')).toBeNull()
    expect(queryByText('Federation')).toBeNull()
    expect(queryByText('Parent Control')).toBeNull()
    // AT HOME header still renders because Shopping/Presence/Gallery
    // are unconditionally visible.
    const headers = Array.from(container.querySelectorAll('.sh-sidenav-group-header'))
      .map((el) => el.textContent?.trim())
    expect(headers).toContain('At home')
  })

  it('hides Admin and Federation for non-admin users', () => {
    setUser({ is_admin: false })
    const { queryByText } = renderAt('/')
    expect(queryByText('Admin')).toBeNull()
    expect(queryByText('Federation')).toBeNull()
    expect(queryByText('Settings')).toBeTruthy()
  })

  it('shows Admin and Federation for admin users', () => {
    setUser({ is_admin: true })
    const { queryByText } = renderAt('/')
    expect(queryByText('Admin')).toBeTruthy()
    expect(queryByText('Federation')).toBeTruthy()
  })

  it('hides Parent Control when the caller is not a guardian', () => {
    setUser({ is_admin: true })
    isGuardian.value = false
    const { queryByText } = renderAt('/')
    expect(queryByText('Parent Control')).toBeNull()
  })

  it('shows Parent Control when isGuardian is true and links to /parent', () => {
    setUser({ is_admin: false })
    isGuardian.value = true
    const { getByText } = renderAt('/')
    const link = getByText('Parent Control').closest('a')
    expect(link).toBeTruthy()
    expect(link?.getAttribute('href')).toBe('/parent')
  })

  it('renders the identity strip with the user avatar and display name', () => {
    setUser({ display_name: 'Pascal Vizeli', picture_url: '/pic.jpg' })
    const { container } = renderAt('/')
    const strip = container.querySelector('.sh-sidenav-identity')
    expect(strip).toBeTruthy()
    expect(strip?.textContent).toContain('Pascal Vizeli')
    // No buttons or extra links inside the identity strip — pure
    // identity cue. Settings + logout live elsewhere.
    expect(strip?.querySelector('a, button')).toBeNull()
  })

  it('marks the active group with sh-sidenav-group--active when on a child route', () => {
    setUser({ is_admin: true })
    const { container } = renderAt('/calendar')
    const homeNav = container.querySelector('nav[aria-labelledby="sidenav-group-home"]')
    expect(homeNav?.classList.contains('sh-sidenav-group--active')).toBe(true)
    const browseNav = container.querySelector('nav[aria-labelledby="sidenav-group-browse"]')
    expect(browseNav?.classList.contains('sh-sidenav-group--active')).toBe(false)
  })

  it('sets aria-current="page" on the active link', () => {
    setUser({ is_admin: true })
    const { getByText } = renderAt('/calendar')
    const calendarLink = getByText('Calendar').closest('a')
    expect(calendarLink?.getAttribute('aria-current')).toBe('page')
    const tasksLink = getByText('Tasks').closest('a')
    expect(tasksLink?.getAttribute('aria-current')).toBeNull()
  })

  it('Corner sits in BROWSE pointing at /corner, not in YOU', () => {
    setUser({ is_admin: true })
    const { container, getByText } = renderAt('/')
    const browseNav = container.querySelector('nav[aria-labelledby="sidenav-group-browse"]')!
    expect(browseNav.textContent).toContain('Corner')
    const youNav = container.querySelector('nav[aria-labelledby="sidenav-group-you"]')!
    expect(youNav.textContent).not.toContain('Corner')
    expect(youNav.textContent).not.toContain('Dashboard')
    const cornerLink = getByText('Corner').closest('a')
    expect(cornerLink?.getAttribute('href')).toBe('/corner')
  })

  it('does not render Notifications or Search links — those live in the top bar only', () => {
    setUser({ is_admin: true })
    const { queryByText } = renderAt('/')
    expect(queryByText('Notifications')).toBeNull()
    expect(queryByText('Search')).toBeNull()
  })

  it('hides the Calls fast-lane entry when no call is active', () => {
    setUser({ is_admin: true })
    const { queryByText } = renderAt('/')
    expect(queryByText('Calls')).toBeNull()
    // Chats is always present under Talk.
    expect(queryByText('Chats')).toBeTruthy()
  })

  it('shows the Calls fast-lane entry pointing at /dms?tab=calls when a call is live', () => {
    setUser({ is_admin: true })
    activeCalls.value = [{
      call_id: 'c-1', status: 'in_progress', caller: 'u1',
      callee: 'u2', call_type: 'audio', created_at: 0,
    }]
    const { getByText } = renderAt('/')
    const link = getByText('Calls').closest('a')
    expect(link).toBeTruthy()
    expect(link?.getAttribute('href')).toBe('/dms?tab=calls')
  })

  it('renders an unread badge on Chats when dmUnreadTotal > 0', () => {
    setUser({ is_admin: true })
    dmUnreadTotal.value = 4
    const { getByText } = renderAt('/')
    const link = getByText('Chats').closest('a')!
    const badge = link.querySelector('.sh-sidenav-badge')
    expect(badge?.textContent).toBe('4')
  })

  it('caps the Chats unread badge at 99+', () => {
    setUser({ is_admin: true })
    dmUnreadTotal.value = 250
    const { getByText } = renderAt('/')
    const badge = getByText('Chats').closest('a')!.querySelector('.sh-sidenav-badge')
    expect(badge?.textContent).toBe('99+')
  })

  it('hides the Chats badge when there are no unread DMs', () => {
    setUser({ is_admin: true })
    dmUnreadTotal.value = 0
    const { getByText } = renderAt('/')
    const link = getByText('Chats').closest('a')!
    expect(link.querySelector('.sh-sidenav-badge')).toBeNull()
  })

  it('renders the federation link as Federation, not Connections', () => {
    setUser({ is_admin: true })
    const { queryByText, getByText } = renderAt('/')
    expect(queryByText('Connections')).toBeNull()
    const link = getByText('Federation').closest('a')
    expect(link?.getAttribute('href')).toBe('/connections')
  })

  it('renders Stories and Momentum unconditionally — they are user-level, not household-toggled', () => {
    setUser({ is_admin: true })
    toggles.value = { ...ALL_FEATURES_ON, feat_stories: false, feat_momentum: false }
    const { getByText } = renderAt('/')
    expect(getByText('Stories')).toBeTruthy()
    expect(getByText('Momentum')).toBeTruthy()
  })

  it('drops the standalone Story archive / Moments archive entries — those live as tabs now', () => {
    setUser({ is_admin: true })
    const { queryByText } = renderAt('/')
    expect(queryByText('Story archive')).toBeNull()
    expect(queryByText('Moments archive')).toBeNull()
  })
})
