import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { FileRenderer, VideoRenderer, ImageRenderer } from './FileRenderer'

describe('FileRenderer', () => {
  it('renders file name and download link', () => {
    const { container, getByText } = render(
      <FileRenderer file={{ url: '/f.pdf', mime_type: 'application/pdf', original_name: 'spec.pdf', size_bytes: 2048 }} />
    )
    expect(getByText('spec.pdf')).toBeTruthy()
    expect(container.querySelector('a')?.getAttribute('href')).toBe('/f.pdf')
  })

  it('formats file size correctly', () => {
    const { getByText } = render(
      <FileRenderer file={{ url: '/f', mime_type: 'text/plain', original_name: 'x', size_bytes: 1536 }} />
    )
    expect(getByText('1.5 KB')).toBeTruthy()
  })
})

describe('VideoRenderer', () => {
  it('renders video element when ready (status absent)', () => {
    const { container } = render(<VideoRenderer src="/v.mp4" />)
    expect(container.querySelector('video')).toBeTruthy()
  })

  it('renders the processing placeholder (no <video>) while transcoding', () => {
    const { container } = render(
      <VideoRenderer src="/v.webm" mediaStatus="processing" />,
    )
    expect(container.querySelector('video')).toBeNull()
    expect(container.querySelector('.sh-video-processing')).toBeTruthy()
  })

  it('renders the player when mediaStatus is ready', () => {
    const { container } = render(
      <VideoRenderer src="/v2.webm" mediaStatus="ready" />,
    )
    expect(container.querySelector('video')).toBeTruthy()
  })
})

describe('ImageRenderer', () => {
  it('renders image element', () => {
    const { container } = render(<ImageRenderer src="/i.webp" />)
    const img = container.querySelector('img')
    expect(img?.getAttribute('src')).toBe('/i.webp')
  })
})
