/**
 * Tests for VideoMedia — the status-aware video renderer.
 *
 * Covers the four render branches: processing placeholder, the
 * WS-driven swap to the player when ``markMediaReady`` flags the src's
 * filename, the immediate player for ready/undefined status, and the
 * failed state.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render } from '@testing-library/preact'

// VideoMedia → store/mediaReady → @/ws. Stub the WS so the store loads
// without a real socket; the test drives readiness via markMediaReady.
vi.mock('@/ws', () => ({ ws: { on: () => () => {} } }))

import { VideoMedia } from './VideoMedia'
import { markMediaReady, _resetMediaReadyForTest } from '@/store/mediaReady'

const SRC = 'api/media/abc.webm?exp=1&sig=xyz'
const POSTER = 'api/media/abc-thumb.jpg?sig=p'

describe('VideoMedia', () => {
  beforeEach(() => { _resetMediaReadyForTest() })

  it('renders the processing placeholder (no <video>) while processing', () => {
    const { container, getByText } = render(
      <VideoMedia src={SRC} poster={POSTER} mediaStatus="processing" />,
    )
    expect(container.querySelector('video')).toBeNull()
    expect(container.querySelector('.sh-video-processing')).not.toBeNull()
    expect(getByText(/Processing video/)).toBeTruthy()
  })

  it('swaps to a <video> when the WS frame marks the src filename ready', () => {
    markMediaReady('abc.webm')
    const { container } = render(
      <VideoMedia src={SRC} poster={POSTER} mediaStatus="processing" />,
    )
    const video = container.querySelector('video')
    expect(video).not.toBeNull()
    expect(video!.getAttribute('src')).toBe(SRC)
    expect(video!.getAttribute('poster')).toBe(POSTER)
    expect(video!.hasAttribute('controls')).toBe(true)
  })

  it('renders a <video> immediately for ready status', () => {
    const { container } = render(
      <VideoMedia src={SRC} poster={POSTER} mediaStatus="ready" />,
    )
    expect(container.querySelector('video')).not.toBeNull()
  })

  it('renders a <video> immediately for undefined status (older payloads)', () => {
    const { container } = render(<VideoMedia src={SRC} poster={POSTER} />)
    const video = container.querySelector('video')
    expect(video).not.toBeNull()
    expect(video!.getAttribute('src')).toBe(SRC)
  })

  it('renders the failed state for failed status', () => {
    const { container, getByText } = render(
      <VideoMedia src={SRC} poster={POSTER} mediaStatus="failed" />,
    )
    expect(container.querySelector('video')).toBeNull()
    expect(container.querySelector('.sh-video-failed')).not.toBeNull()
    expect(getByText(/couldn’t be processed/)).toBeTruthy()
  })

  it('a WS-ready frame overrides a stale failed status', () => {
    markMediaReady('abc.webm')
    const { container } = render(
      <VideoMedia src={SRC} poster={POSTER} mediaStatus="failed" />,
    )
    expect(container.querySelector('video')).not.toBeNull()
  })
})
