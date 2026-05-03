import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

vi.mock('@/api', () => {
  const m = { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
  return { api: m }
})
vi.mock('./Toast', () => ({ showToast: vi.fn() }))

const routeSpy = vi.fn()
vi.mock('preact-iso', () => ({
  useLocation: () => ({ route: routeSpy, url: '/' }),
}))

import { CallTypePickerDialog, openCallTypePicker } from './CallTypePickerDialog'
import { api } from '@/api'

const apiMock = api as unknown as {
  post: ReturnType<typeof vi.fn>
}

describe('CallTypePickerDialog', () => {
  beforeEach(() => {
    apiMock.post.mockReset()
    routeSpy.mockReset()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
  })

  it('renders nothing when closed', () => {
    const { container } = render(<CallTypePickerDialog />)
    expect(container.querySelector('.sh-call-picker')).toBeNull()
  })

  it('renders one audio + one video tile when opened', async () => {
    openCallTypePicker('conv-1')
    const { findByLabelText } = render(<CallTypePickerDialog />)
    expect(await findByLabelText('Start audio call')).toBeTruthy()
    expect(await findByLabelText('Start video call')).toBeTruthy()
  })

  it('POSTs an audio call when the Audio tile is clicked', async () => {
    apiMock.post.mockResolvedValueOnce({ call_id: 'call-aud' })
    openCallTypePicker('conv-aud')
    const { findByLabelText } = render(<CallTypePickerDialog />)
    const tile = await findByLabelText('Start audio call')
    fireEvent.click(tile)
    await new Promise(r => setTimeout(r, 0))
    expect(apiMock.post).toHaveBeenCalledWith('/api/calls', {
      conversation_id: 'conv-aud',
      call_type: 'audio',
      sdp_offer: 'v=0\r\n',
    })
    expect(routeSpy).toHaveBeenCalledWith('/calls/call-aud')
  })

  it('POSTs a video call when the Video tile is clicked', async () => {
    apiMock.post.mockResolvedValueOnce({ call_id: 'call-vid' })
    openCallTypePicker('conv-vid')
    const { findByLabelText } = render(<CallTypePickerDialog />)
    const tile = await findByLabelText('Start video call')
    fireEvent.click(tile)
    await new Promise(r => setTimeout(r, 0))
    expect(apiMock.post).toHaveBeenCalledWith('/api/calls', {
      conversation_id: 'conv-vid',
      call_type: 'video',
      sdp_offer: 'v=0\r\n',
    })
    expect(routeSpy).toHaveBeenCalledWith('/calls/call-vid')
  })
})
