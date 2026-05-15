import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { AudioBubble } from './AudioBubble'

describe('AudioBubble', () => {
  it('renders an audio element with the src + preload="metadata"', () => {
    const { container } = render(
      <AudioBubble src="api/media/v.ogg" transcript="" />,
    )
    const audio = container.querySelector('audio')
    expect(audio).toBeTruthy()
    expect(audio?.getAttribute('src')).toBe('api/media/v.ogg')
    expect(audio?.getAttribute('preload')).toBe('metadata')
  })

  it('shows a "Transcribing…" placeholder when transcript is empty', () => {
    const { getByText } = render(
      <AudioBubble src="api/media/v.ogg" transcript="" />,
    )
    expect(getByText('Transcribing…')).toBeTruthy()
  })

  it('renders the transcript text when present', () => {
    const { getByText, queryByText } = render(
      <AudioBubble src="api/media/v.ogg" transcript="hello there" />,
    )
    expect(getByText('hello there')).toBeTruthy()
    expect(queryByText('Transcribing…')).toBeNull()
  })

  it('exposes the filename as aria-label on the player', () => {
    const { container } = render(
      <AudioBubble
        src="api/media/v.ogg"
        transcript=""
        fileName="voice-note-123.ogg"
      />,
    )
    expect(container.querySelector('audio')?.getAttribute('aria-label')).toBe(
      'voice-note-123.ogg',
    )
  })

  it('falls back to "Voice note" when no filename is supplied', () => {
    const { container } = render(
      <AudioBubble src="api/media/v.ogg" transcript="" />,
    )
    expect(container.querySelector('audio')?.getAttribute('aria-label')).toBe(
      'Voice note',
    )
  })

  it('adds the pending modifier class when sync is in-flight', () => {
    const { container } = render(
      <AudioBubble src="api/media/v.ogg" transcript="" pending />,
    )
    expect(
      container.querySelector('.sh-message-audio__player--pending'),
    ).toBeTruthy()
  })
})
