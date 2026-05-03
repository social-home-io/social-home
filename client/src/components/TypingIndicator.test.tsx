import { describe, it, expect, vi, beforeEach } from 'vitest'

const { wsSendMock } = vi.hoisted(() => ({ wsSendMock: vi.fn() }))

vi.mock('@/ws', () => ({
  ws: {
    send: wsSendMock,
    on: vi.fn(() => () => {}),
  },
}))

import { sendTyping } from './TypingIndicator'

describe('TypingIndicator', () => {
  beforeEach(() => {
    wsSendMock.mockReset()
  })

  it('module exports exist', async () => {
    const mod = await import('./TypingIndicator')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  it('sendTyping(string) routes to a DM conversation_id frame', () => {
    sendTyping('conv-1')
    expect(wsSendMock).toHaveBeenCalledOnce()
    const [type, payload] = wsSendMock.mock.calls[0]
    expect(type).toBe('typing')
    expect(payload).toEqual({ conversation_id: 'conv-1' })
  })

  it('sendTyping({postId}) routes to a comment-thread frame (no space)', () => {
    sendTyping({ postId: 'post-7' })
    const [, payload] = wsSendMock.mock.calls[0]
    expect(payload).toEqual({ post_id: 'post-7' })
    expect('space_id' in payload).toBe(false)
  })

  it('sendTyping({postId, spaceId}) carries the space scope', () => {
    sendTyping({ postId: 'post-7', spaceId: 'space-x' })
    const [, payload] = wsSendMock.mock.calls[0]
    expect(payload).toEqual({ post_id: 'post-7', space_id: 'space-x' })
  })

  it('sendTyping({postId, spaceId: null}) omits space_id', () => {
    sendTyping({ postId: 'post-7', spaceId: null })
    const [, payload] = wsSendMock.mock.calls[0]
    expect(payload).toEqual({ post_id: 'post-7' })
  })
})
