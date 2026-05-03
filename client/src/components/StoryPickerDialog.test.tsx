import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'

vi.mock('@/api', () => {
  const m = { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
  return { api: m }
})
vi.mock('./Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', display_name: 'Pascal' } },
}))

import { StoryPickerDialog, openStoryPicker } from './StoryPickerDialog'
import { api } from '@/api'

const apiMock = api as unknown as { get: ReturnType<typeof vi.fn> }

describe('StoryPickerDialog', () => {
  beforeEach(() => {
    apiMock.get.mockReset()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
  })

  it('renders nothing when closed', () => {
    const { container } = render(<StoryPickerDialog />)
    expect(container.querySelector('.sh-story-picker')).toBeNull()
  })

  it('opens with a loading state and lists my stories', async () => {
    apiMock.get.mockResolvedValueOnce([
      {
        story: {
          id: 's1',
          author_user_id: 'u1',
          story_date: '2026-05-03',
          audience_kind: 'all_paired',
          audience: [],
          created_at: '2026-05-03T08:00:00Z',
          expires_at: '2026-06-02T08:00:00Z',
        },
        frames: [
          {
            id: 'f1',
            story_id: 's1',
            sequence: 1,
            frame_type: 'image',
            media_url: '/api/media/x.webp',
            caption_text: null,
            caption_emoji: null,
            duration_ms: null,
            created_at: '2026-05-03T08:00:00Z',
          },
        ],
        unseen_count: 0,
      },
    ])
    openStoryPicker({ scope: 'household' })
    const { findByText } = render(<StoryPickerDialog />)
    expect(await findByText('2026-05-03')).toBeTruthy()
  })
})
