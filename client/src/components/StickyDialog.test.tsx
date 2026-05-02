import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

vi.mock('@/api', () => {
  const m = { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
  return { api: m, _mock: m }
})

vi.mock('./Toast', () => ({ showToast: vi.fn() }))

import {
  StickyDialog,
  STICKY_COLORS,
  openCreateStickyDialog,
  openEditStickyDialog,
} from './StickyDialog'
import { api } from '@/api'
import { stickies } from '@/store/stickies'

const apiMock = api as unknown as {
  get: ReturnType<typeof vi.fn>; post: ReturnType<typeof vi.fn>;
  patch: ReturnType<typeof vi.fn>; delete: ReturnType<typeof vi.fn>;
}

function fakeSticky(over: Partial<{ id: string; content: string; color: string }> = {}) {
  return {
    id: over.id ?? 's-1',
    author: 'a-id',
    content: over.content ?? 'Buy milk',
    color: over.color ?? STICKY_COLORS[0],
    position_x: 100,
    position_y: 100,
    created_at: '2026-05-02T12:00:00Z',
    updated_at: '2026-05-02T12:00:00Z',
    space_id: null,
  }
}

describe('StickyDialog', () => {
  beforeEach(() => {
    apiMock.post.mockReset()
    apiMock.patch.mockReset()
    apiMock.delete.mockReset()
    stickies.value = []
    // Force the dialog closed at the start of each test by triggering
    // an Escape on the modal (mounted across tests is fine).
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
  })

  it('renders nothing when closed', () => {
    const { container } = render(<StickyDialog />)
    expect(container.querySelector('.sh-sticky-dialog')).toBeNull()
  })

  it('opens in create mode and shows the swatch picker', async () => {
    const { container } = render(<StickyDialog />)
    openCreateStickyDialog(null, { x: 100, y: 100 }, STICKY_COLORS[2])
    await waitFor(() => {
      expect(container.querySelector('.sh-sticky-dialog')).not.toBeNull()
    })
    // 6 swatches; the seeded one is active.
    expect(
      container.querySelectorAll('.sh-sticky-dialog-swatch').length,
    ).toBe(STICKY_COLORS.length)
    const active = container.querySelector('.sh-sticky-dialog-swatch--active') as HTMLElement
    expect(active).not.toBeNull()
    // jsdom normalises hex to rgb(); check parity by RGB integers.
    expect(active.style.background).toContain('179, 255, 179')
  })

  it('opens in edit mode pre-filled with sticky content', async () => {
    const { container } = render(<StickyDialog />)
    openEditStickyDialog(fakeSticky({ content: 'Pickup laundry' }), null)
    await waitFor(() => {
      expect(container.querySelector('.sh-sticky-dialog')).not.toBeNull()
    })
    const ta = container.querySelector('textarea') as HTMLTextAreaElement
    expect(ta.value).toBe('Pickup laundry')
  })

  it('create submit POSTs to household endpoint', async () => {
    apiMock.post.mockResolvedValueOnce(fakeSticky({ id: 's-new', content: 'Hi' }))
    const { container } = render(<StickyDialog />)
    openCreateStickyDialog(null, { x: 200, y: 100 })
    await waitFor(() => {
      expect(container.querySelector('.sh-sticky-dialog')).not.toBeNull()
    })
    const ta = container.querySelector('textarea') as HTMLTextAreaElement
    fireEvent.input(ta, { target: { value: 'Hi' } })
    const form = container.querySelector('form') as HTMLFormElement
    fireEvent.submit(form)
    await waitFor(() => {
      expect(apiMock.post).toHaveBeenCalled()
    })
    expect(apiMock.post.mock.calls[0][0]).toBe('/api/stickies')
  })

  it('create submit POSTs to the space-scoped endpoint when spaceId is set', async () => {
    apiMock.post.mockResolvedValueOnce(fakeSticky({ id: 's-new', content: 'Hi' }))
    const { container } = render(<StickyDialog />)
    openCreateStickyDialog('sp-42', { x: 0, y: 0 })
    await waitFor(() => {
      expect(container.querySelector('.sh-sticky-dialog')).not.toBeNull()
    })
    const ta = container.querySelector('textarea') as HTMLTextAreaElement
    fireEvent.input(ta, { target: { value: 'Space note' } })
    fireEvent.submit(container.querySelector('form') as HTMLFormElement)
    await waitFor(() => {
      expect(apiMock.post).toHaveBeenCalled()
    })
    expect(apiMock.post.mock.calls[0][0]).toBe('/api/spaces/sp-42/stickies')
  })

  it('edit submit PATCHes content + colour', async () => {
    apiMock.patch.mockResolvedValueOnce(
      fakeSticky({ id: 's-1', content: 'New text', color: STICKY_COLORS[3] }),
    )
    const { container } = render(<StickyDialog />)
    openEditStickyDialog(fakeSticky(), null)
    await waitFor(() => {
      expect(container.querySelector('.sh-sticky-dialog')).not.toBeNull()
    })
    // Change text + pick a different colour.
    const ta = container.querySelector('textarea') as HTMLTextAreaElement
    fireEvent.input(ta, { target: { value: 'New text' } })
    const swatches = container.querySelectorAll('.sh-sticky-dialog-swatch')
    fireEvent.click(swatches[3])
    fireEvent.submit(container.querySelector('form') as HTMLFormElement)
    await waitFor(() => {
      expect(apiMock.patch).toHaveBeenCalled()
    })
    const [url, body] = apiMock.patch.mock.calls[0]
    expect(url).toBe('/api/stickies/s-1')
    expect(body.content).toBe('New text')
    expect(body.color).toBe(STICKY_COLORS[3])
  })

  it('edit dialog shows a Delete button (not present on create)', async () => {
    const { container } = render(<StickyDialog />)
    openCreateStickyDialog(null)
    await waitFor(() => {
      expect(container.querySelector('.sh-sticky-dialog')).not.toBeNull()
    })
    expect(container.textContent).not.toContain('Delete')
    openEditStickyDialog(fakeSticky(), null)
    await waitFor(() => {
      expect(container.textContent).toContain('Delete')
    })
  })
})
