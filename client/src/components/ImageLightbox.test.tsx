import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

vi.mock('./Toast', () => ({ showToast: vi.fn() }))

import {
  ImageLightbox,
  copyReferenceForItem,
  openLightbox,
  closeLightbox,
} from './ImageLightbox'

describe('ImageLightbox', () => {
  it('module exports exist', async () => {
    const mod = await import('./ImageLightbox')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  describe('copyReferenceForItem', () => {
    beforeEach(() => {
      Object.defineProperty(navigator, 'clipboard', {
        value: { writeText: vi.fn(async () => undefined) },
        configurable: true,
      })
    })

    it('copies a markdown image with the canonical /api/media URL', async () => {
      await copyReferenceForItem({
        url: '/api/media/abc.webp?exp=999&sig=DEADBEEF',
        caption: 'Sunrise',
      })
      const writeText = navigator.clipboard.writeText as unknown as ReturnType<
        typeof vi.fn
      >
      expect(writeText).toHaveBeenCalledOnce()
      const arg = writeText.mock.calls[0][0] as string
      expect(arg).toBe('![Sunrise](/api/media/abc.webp)')
      expect(arg).not.toContain('?exp=')
      expect(arg).not.toContain('&sig=')
    })

    it('falls back to a filename-derived alt when no caption', async () => {
      await copyReferenceForItem({
        url: '/api/media/holiday-photo.webp?exp=1&sig=2',
      })
      const writeText = navigator.clipboard.writeText as unknown as ReturnType<
        typeof vi.fn
      >
      expect(writeText.mock.calls[0][0]).toBe(
        '![holiday-photo](/api/media/holiday-photo.webp)',
      )
    })
  })

  describe('Copy reference button', () => {
    beforeEach(() => {
      closeLightbox()
      Object.defineProperty(navigator, 'clipboard', {
        value: { writeText: vi.fn(async () => undefined) },
        configurable: true,
      })
    })

    it('renders in the lightbox toolbar and copies on click', async () => {
      openLightbox({
        items: [
          {
            url: '/api/media/x.webp?exp=1&sig=2',
            caption: 'Lunch',
            item_type: 'photo',
          },
        ],
        index: 0,
      })
      const { findByRole } = render(<ImageLightbox />)
      const btn = await findByRole('button', { name: /copy.*reference/i })
      fireEvent.click(btn)
      await new Promise(r => setTimeout(r, 0))
      const writeText = navigator.clipboard.writeText as unknown as ReturnType<
        typeof vi.fn
      >
      expect(writeText).toHaveBeenCalledOnce()
      expect(writeText.mock.calls[0][0]).toBe('![Lunch](/api/media/x.webp)')
    })
  })
})
