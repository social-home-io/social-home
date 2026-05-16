import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { MediaAttachmentChip } from './MediaAttachmentChip'

describe('MediaAttachmentChip', () => {
  it('renders the uploading phase with a percent label + progress ring', () => {
    const { container, getByText } = render(
      <MediaAttachmentChip
        phase="uploading"
        kind="image"
        filename="photo.jpg"
        previewUrl="blob:fake"
        percent={42}
      />,
    )
    expect(getByText(/Uploading… 42%/)).toBeTruthy()
    expect(container.querySelector('.sh-attach-chip__ring')).toBeTruthy()
  })

  it('renders the processing phase with kind-specific copy and a spinner', () => {
    const { container, getByText } = render(
      <MediaAttachmentChip
        phase="processing"
        kind="video"
        filename="clip.mp4"
        previewUrl="blob:fake"
        percent={100}
      />,
    )
    expect(getByText(/Processing video/i)).toBeTruthy()
    expect(container.querySelector('.sh-attach-chip__spinner')).toBeTruthy()
    expect(container.querySelector('.sh-attach-chip__ring')).toBeNull()
  })

  it('renders the ready phase with a size caption and no overlay', () => {
    const { container, getByText } = render(
      <MediaAttachmentChip
        phase="ready"
        kind="image"
        filename="photo.jpg"
        previewUrl="https://test/api/media/photo.webp"
        sizeBytes={1_200_000}
      />,
    )
    expect(getByText('1.1 MB')).toBeTruthy()
    expect(container.querySelector('.sh-attach-chip__overlay')).toBeNull()
  })

  it('renders the failed phase with the error message and a Retry button', () => {
    const onRetry = vi.fn()
    const { getByText } = render(
      <MediaAttachmentChip
        phase="failed"
        kind="image"
        filename="photo.jpg"
        previewUrl="blob:fake"
        errorMessage="Network broke"
        onRetry={onRetry}
      />,
    )
    expect(getByText(/Upload failed/i)).toBeTruthy()
    expect(getByText(/Network broke/)).toBeTruthy()
    fireEvent.click(getByText('Retry'))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('clear button invokes onClear with the right aria label per phase', () => {
    const onClear = vi.fn()
    const { container } = render(
      <MediaAttachmentChip
        phase="uploading"
        kind="image"
        filename="photo.jpg"
        previewUrl="blob:fake"
        percent={20}
        onClear={onClear}
      />,
    )
    const btn = container.querySelector('.sh-attach-chip__clear') as HTMLButtonElement
    expect(btn.getAttribute('aria-label')).toMatch(/Cancel upload/i)
    fireEvent.click(btn)
    expect(onClear).toHaveBeenCalledTimes(1)
  })

  it('renders the file kind as a glyph instead of an <img>', () => {
    const { container } = render(
      <MediaAttachmentChip
        phase="ready"
        kind="file"
        filename="invoice.pdf"
        previewUrl={null}
        sizeBytes={350_000}
      />,
    )
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('.sh-attach-chip__glyph')).toBeTruthy()
  })

  it('sets aria-busy while in flight', () => {
    const { container, rerender } = render(
      <MediaAttachmentChip
        phase="uploading"
        kind="image"
        filename="x.jpg"
        previewUrl="blob:fake"
        percent={10}
      />,
    )
    expect(container.querySelector('[aria-busy="true"]')).toBeTruthy()
    rerender(
      <MediaAttachmentChip
        phase="ready"
        kind="image"
        filename="x.jpg"
        previewUrl="blob:fake"
        sizeBytes={100}
      />,
    )
    expect(container.querySelector('[aria-busy="true"]')).toBeNull()
  })
})
