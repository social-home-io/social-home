import { describe, it, expect } from 'vitest'
import { describeUploadError } from './uploadErrors'

const file = (name: string, sizeMb: number): File => {
  const blob = new Blob([new Uint8Array(sizeMb * 1024 * 1024)])
  return new File([blob], name, { type: 'image/jpeg' })
}

describe('describeUploadError', () => {
  it('detects 413 too-large', () => {
    const msg = describeUploadError(
      new Error('Upload failed (413): too big'),
      { file: file('cat.jpg', 32) },
    )
    expect(msg).toMatch(/too large/i)
    expect(msg).toContain('cat.jpg')
    expect(msg).toContain('32 MB')
  })

  it('detects 415 unsupported type', () => {
    const msg = describeUploadError(new Error('API 415: /api/media/upload'))
    expect(msg).toMatch(/file type isn't supported/i)
  })

  it('detects 429 throttle', () => {
    const msg = describeUploadError(new Error('API 429: /api/media/upload'))
    expect(msg).toMatch(/uploaded a lot recently/i)
  })

  it('detects network failure', () => {
    const msg = describeUploadError(new TypeError('Failed to fetch'))
    expect(msg).toMatch(/couldn't reach the server/i)
  })

  it('falls back to a trimmed raw message when nothing matches', () => {
    const msg = describeUploadError(new Error('disk on fire'))
    expect(msg).toContain('disk on fire')
  })

  it('handles non-Error values', () => {
    const msg = describeUploadError('boom')
    expect(msg).toContain('boom')
  })

  it('uses the file name in the lead when provided', () => {
    const msg = describeUploadError(new Error('boom'), {
      file: file('trip.mp4', 1),
    })
    expect(msg.startsWith("Couldn't upload trip.mp4")).toBe(true)
  })
})
