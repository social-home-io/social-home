import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'

vi.mock('@/api', () => {
  const m = { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
  return { api: m }
})
vi.mock('preact-iso', () => ({
  useLocation: () => ({ route: vi.fn(), url: '/' }),
}))

import { HighlightShareCard } from './HighlightShareCard'
import { api } from '@/api'

const apiMock = api as unknown as { get: ReturnType<typeof vi.fn> }

describe('HighlightShareCard', () => {
  beforeEach(() => {
    apiMock.get.mockReset()
  })

  it('renders the "ended" placeholder when highlightId is null', () => {
    const { getByText } = render(<HighlightShareCard highlightId={null} />)
    expect(getByText('Highlight has ended')).toBeTruthy()
  })

  it('renders the share-note alongside the placeholder', () => {
    const { getByText } = render(
      <HighlightShareCard highlightId={null} note="From last summer" />,
    )
    expect(getByText('From last summer')).toBeTruthy()
  })
})
