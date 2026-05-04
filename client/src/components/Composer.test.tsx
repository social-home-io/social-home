import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

beforeEach(() => {
  vi.resetModules()
})

function commonMocks() {
  vi.doMock('@/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
  vi.doMock('@/store/auth', () => ({
    currentUser: { value: { username: 'pascal', display_name: 'Pascal' } },
  }))
  vi.doMock('./Toast', () => ({ showToast: vi.fn() }))
}

describe('Composer', () => {
  it('module exports exist', async () => {
    commonMocks()
    const mod = await import('./Composer')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  it('hides the bazaar option when not in a space', async () => {
    commonMocks()
    const { Composer } = await import('./Composer')
    const { queryByLabelText } = render(<Composer onSubmit={vi.fn()} />)
    expect(queryByLabelText('Text post')).toBeTruthy()
    expect(queryByLabelText('Poll')).toBeTruthy()
    expect(queryByLabelText('Bazaar listing')).toBeNull()
  })

  it('exposes the bazaar option inside a space', async () => {
    commonMocks()
    const { Composer } = await import('./Composer')
    const { queryByLabelText } = render(
      <Composer onSubmit={vi.fn()} spaceId="space-1" />,
    )
    expect(queryByLabelText('Bazaar listing')).toBeTruthy()
  })

  it('hides the textarea when poll/schedule is picked (builder modes)', async () => {
    commonMocks()
    const { Composer } = await import('./Composer')
    const { queryByPlaceholderText, getByLabelText } = render(
      <Composer onSubmit={vi.fn()} />,
    )
    expect(queryByPlaceholderText(/What's on your mind/)).toBeTruthy()
    fireEvent.click(getByLabelText('Poll'))
    expect(queryByPlaceholderText(/What's on your mind/)).toBeNull()
    fireEvent.click(getByLabelText('Schedule'))
    expect(queryByPlaceholderText(/What's on your mind/)).toBeNull()
    fireEvent.click(getByLabelText('Text post'))
    expect(queryByPlaceholderText(/What's on your mind/)).toBeTruthy()
  })
})
