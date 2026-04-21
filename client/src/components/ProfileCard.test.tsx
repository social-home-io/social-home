import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { ProfileCard } from './ProfileCard'
import type { User } from '@/types'

const user: User = {
  user_id: 'u1',
  username: 'anna',
  display_name: 'Anna',
  is_admin: true,
  picture_url: null,
  bio: 'Builder of things',
  is_new_member: false,
  picture_hash: null,
}

describe('ProfileCard', () => {
  it('shows display name and username', () => {
    const { getByText } = render(<ProfileCard user={user} onClose={() => {}} />)
    expect(getByText('Anna')).toBeTruthy()
    expect(getByText('@anna')).toBeTruthy()
  })

  it('shows admin badge', () => {
    const { container } = render(<ProfileCard user={user} onClose={() => {}} />)
    expect(container.textContent).toContain('Admin')
  })

  it('shows bio', () => {
    const { getByText } = render(<ProfileCard user={user} onClose={() => {}} />)
    expect(getByText('Builder of things')).toBeTruthy()
  })

  it('calls onClose when X clicked', () => {
    const fn = vi.fn()
    const { container } = render(<ProfileCard user={user} onClose={fn} />)
    const btn = container.querySelector('.sh-profile-close')
    if (btn) fireEvent.click(btn)
    expect(fn).toHaveBeenCalledOnce()
  })

  it('shows DM button when onDm provided', () => {
    const fn = vi.fn()
    const { getByText } = render(<ProfileCard user={user} onDm={fn} onClose={() => {}} />)
    fireEvent.click(getByText('Message'))
    expect(fn).toHaveBeenCalledOnce()
  })
})
