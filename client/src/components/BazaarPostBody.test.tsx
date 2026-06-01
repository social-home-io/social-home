import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

const { apiGet } = vi.hoisted(() => ({ apiGet: vi.fn() }))
vi.mock('@/api', () => ({
  api: { get: apiGet, post: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/ws', () => ({ ws: { on: () => () => {} } }))
vi.mock('@/store/auth', () => ({ currentUser: { value: { user_id: 'seller1' } } }))
vi.mock('./Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/components/confirm', () => ({ confirmDialog: vi.fn() }))
vi.mock('./BazaarOffersPanel', () => ({ BazaarOffersPanel: () => null }))
vi.mock('./SaveListingButton', () => ({ SaveListingButton: () => null }))
vi.mock('./FileRenderer', () => ({ ImageRenderer: () => null }))

import { BazaarPostBody } from './BazaarPostBody'

const future = () => new Date(Date.now() + 86_400_000).toISOString()

function listing(over: Record<string, unknown> = {}) {
  return {
    post_id: 'p1', space_id: 's1', seller_user_id: 'seller1', mode: 'offer',
    title: 'Vinyl', end_time: future(), currency: 'EUR', status: 'active',
    created_at: '2026-01-01', image_urls: [], ...over,
  }
}

function mockApi(l: object, bids: object[], offers: object[]) {
  apiGet.mockImplementation((url: string) => {
    const u = String(url ?? '')
    if (u.endsWith('/bids')) return Promise.resolve(bids)
    if (u.endsWith('/offers')) return Promise.resolve(offers)
    return Promise.resolve(l)
  })
}

describe('BazaarPostBody activity count', () => {
  beforeEach(() => apiGet.mockReset())

  it('counts OFFERS from the offers table for offer mode (regression: was reading bids → always 0)', async () => {
    mockApi(
      listing({ mode: 'offer' }),
      [],  // no bids
      [{ id: 'o1', listing_post_id: 'p1', offerer_user_id: 'buyer1',
         amount: 4000, status: 'pending', created_at: 'x' }],
    )
    const { container } = render(<BazaarPostBody postId="p1" />)
    await waitFor(() => expect(container.textContent).toContain('1 offer'))
    expect(container.textContent).not.toContain('0 offer')
  })

  it('ignores non-pending offers in the count', async () => {
    mockApi(
      listing({ mode: 'negotiable', price: 1000 }),
      [],
      [
        { id: 'o1', listing_post_id: 'p1', offerer_user_id: 'b1', amount: 1, status: 'rejected', created_at: 'x' },
        { id: 'o2', listing_post_id: 'p1', offerer_user_id: 'b2', amount: 2, status: 'pending', created_at: 'x' },
      ],
    )
    const { container } = render(<BazaarPostBody postId="p1" />)
    await waitFor(() => expect(container.textContent).toContain('1 offer'))
  })

  it('counts BIDS for auction mode', async () => {
    mockApi(
      listing({ mode: 'auction', start_price: 1000 }),
      [{ id: 'b1', listing_post_id: 'p1', bidder_user_id: 'b1', amount: 1100, created_at: 'x' }],
      [],
    )
    const { container } = render(<BazaarPostBody postId="p1" />)
    await waitFor(() => expect(container.textContent).toContain('1 bid'))
  })
})
