import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { PostCard } from './PostCard'
import type { FeedPost } from '@/types'

const mockPost: FeedPost = {
  id: 'p1',
  author: 'anna',
  type: 'text',
  content: 'Hello world!',
  media_url: null,
  file_meta: null,
  reactions: { '👍': ['u1', 'u2'] },
  comment_count: 3,
  pinned: false,
  created_at: new Date().toISOString(),
  edited_at: null,
}

describe('PostCard', () => {
  it('renders post content', () => {
    const { getByText } = render(<PostCard post={mockPost} />)
    expect(getByText('Hello world!')).toBeTruthy()
  })

  it('shows reaction counts', () => {
    const { container } = render(<PostCard post={mockPost} />)
    expect(container.textContent).toContain('👍')
    expect(container.textContent).toContain('2')
  })

  it('shows comment count', () => {
    const { container } = render(<PostCard post={mockPost} />)
    expect(container.textContent).toContain('3')
  })

  it('shows pinned badge when pinned', () => {
    const pinned = { ...mockPost, pinned: true }
    const { container } = render(<PostCard post={pinned} />)
    expect(container.textContent).toContain('Pinned')
  })

  it('shows deleted state', () => {
    const deleted = { ...mockPost, content: null }
    const { container } = render(<PostCard post={deleted} />)
    expect(container.textContent).toContain('deleted')
  })

  it('shows edited badge', () => {
    const edited = { ...mockPost, edited_at: new Date().toISOString() }
    const { container } = render(<PostCard post={edited} />)
    expect(container.textContent).toContain('edited')
  })

  it('calls onReact when reaction clicked', () => {
    const fn = vi.fn()
    const { container } = render(<PostCard post={mockPost} onReact={fn} />)
    const addBtn = container.querySelector('.sh-reaction-add')
    if (addBtn) fireEvent.click(addBtn)
    expect(fn).toHaveBeenCalledWith('👍')
  })

  it('calls onComment when comment button clicked', () => {
    const fn = vi.fn()
    const { container } = render(<PostCard post={mockPost} onComment={fn} />)
    const btn = container.querySelector('.sh-comment-btn')
    if (btn) fireEvent.click(btn)
    expect(fn).toHaveBeenCalledOnce()
  })
})
