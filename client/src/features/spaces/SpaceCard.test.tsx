import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { SpaceCard } from './SpaceCard'
import type { DirectoryEntry } from '@/types'

const baseEntry: DirectoryEntry = {
  space_id: 's1', host_instance_id: 'h1',
  host_display_name: 'Nabu Casa', host_is_paired: true,
  name: 'Chess Club', description: 'Weekly chess', emoji: '♟',
  member_count: 7, scope: 'global', join_mode: 'request',
  min_age: 0,
}

describe('SpaceCard', () => {
  it('renders the global scope chip', () => {
    const { getByText } = render(
      <SpaceCard entry={baseEntry} onAction={() => {}} />,
    )
    expect(getByText(/Global/).textContent).toContain('Global')
  })

  it('renders a "Connect first" CTA when host is unpaired', () => {
    const { getByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, host_is_paired: false }}
        onAction={() => {}}
      />,
    )
    expect(getByText(/Connect with/i)).toBeTruthy()
  })

  it('renders "Request pending" disabled when pending', () => {
    const { getByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, request_pending: true }}
        onAction={() => {}}
      />,
    )
    const btn = getByText('Request pending') as HTMLButtonElement
    expect(btn).toBeTruthy()
    expect(btn.closest('button')?.disabled).toBe(true)
  })

  it('renders "Open space" when already a member', () => {
    const { getByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, already_member: true, scope: 'household' }}
        onAction={() => {}}
      />,
    )
    expect(getByText('Open space')).toBeTruthy()
  })

  it('shows age chip when min_age > 0', () => {
    const { getByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, min_age: 13 }}
        onAction={() => {}}
      />,
    )
    expect(getByText('13+')).toBeTruthy()
  })

  it('shows a category chip when category is set and not general', () => {
    const { getByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, category: 'gaming' }}
        onAction={() => {}}
      />,
    )
    expect(getByText('Gaming')).toBeTruthy()
  })

  it('hides the category chip for the general category', () => {
    const { queryByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, category: 'general' }}
        onAction={() => {}}
      />,
    )
    expect(queryByText('General')).toBeNull()
  })

  it('hides the category chip when category is unset', () => {
    const { queryByText } = render(
      <SpaceCard entry={baseEntry} onAction={() => {}} />,
    )
    expect(queryByText('General')).toBeNull()
  })

  // ── Subscribe / unsubscribe ─────────────────────────────────────────

  it('renders a Subscribe button for LOCAL public / global non-members', () => {
    const { getByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, host_instance_id: 'local' }}
        onAction={() => {}}
      />,
    )
    expect(getByText(/Subscribe/)).toBeTruthy()
  })

  it('hides Subscribe for a remotely-hosted (friends / global) space', () => {
    // No remote-subscribe federation path — the button would just 404, so
    // it must not show; remote spaces are joined via the request flow.
    const { queryByText } = render(
      <SpaceCard entry={baseEntry} onAction={() => {}} />,
    )
    expect(queryByText(/Subscribe/)).toBeNull()
  })

  it('does not render a Subscribe button for household-scope entries', () => {
    const { queryByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, scope: 'household' }}
        onAction={() => {}}
      />,
    )
    expect(queryByText(/Subscribe/)).toBeNull()
  })

  it('flips to Unsubscribe when already subscribed', () => {
    const { getByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, already_subscribed: true }}
        onAction={() => {}}
      />,
    )
    expect(getByText(/Unsubscribe/)).toBeTruthy()
    // Subscribed pill also appears in the header.
    expect(getByText(/🔔 Subscribed/)).toBeTruthy()
  })

  it('does not offer Subscribe once the user is a full member', () => {
    const { queryByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, scope: 'public', already_member: true }}
        onAction={() => {}}
      />,
    )
    expect(queryByText(/Subscribe/)).toBeNull()
  })

  it('calls onAction with kind=subscribe on click', () => {
    let captured: { kind: string } | null = null
    const { getByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, host_instance_id: 'local' }}
        onAction={(_e, a) => { captured = a }}
      />,
    )
    ;(getByText(/Subscribe/) as HTMLButtonElement).click()
    expect(captured).toEqual({ kind: 'subscribe' })
  })

  it('disables Subscribe while the parent reports it busy', () => {
    const { getByLabelText } = render(
      <SpaceCard
        entry={{ ...baseEntry, host_instance_id: 'local' }}
        onAction={() => {}}
        subscribeBusy={true}
      />,
    )
    const btn = getByLabelText(/Subscribe to Chess Club/) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })
})
