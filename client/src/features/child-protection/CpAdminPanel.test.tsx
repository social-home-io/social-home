import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'

const apiGet = vi.fn()

vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue({}),
  },
}))
vi.mock('@/ws', () => ({ ws: { on: () => () => {} } }))
vi.mock('@/components/Spinner', () => ({ Spinner: () => null }))

const tick = () => new Promise((r) => setTimeout(r, 0))

describe('CpAdminPanel', () => {
  beforeEach(() => apiGet.mockReset())

  it('module exports exist', async () => {
    const mod = await import('./CpAdminPanel')
    expect(mod).toBeTruthy()
  })

  // Regression: the "Protected" column must reflect /api/cp/protection,
  // NOT user.is_minor — that field is stripped from /api/users as a
  // SENSITIVE_FIELD, so the column was permanently blank before the fix.
  it('shows protection status from /api/cp/protection, not /api/users', async () => {
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/users') {
        return Promise.resolve([
          { user_id: 'u-admin', username: 'admin', display_name: 'Admin' },
          { user_id: 'u-mia', username: 'mia', display_name: 'Mia' },
        ])
      }
      if (path === '/api/cp/protection') {
        return Promise.resolve({
          users: [
            { user_id: 'u-admin', username: 'admin', is_minor: false, declared_age: 0 },
            { user_id: 'u-mia', username: 'mia', is_minor: true, declared_age: 8 },
          ],
        })
      }
      return Promise.resolve([])
    })

    const { default: CpAdminPanel } = await import('./CpAdminPanel')
    const { container } = render(<CpAdminPanel />)
    await tick()
    await tick()

    const text = container.textContent || ''
    // The protected minor is reflected with their declared age.
    expect(text).toContain('🔒 Yes · age 8')
    // The endpoint that actually carries protection state was queried.
    expect(apiGet).toHaveBeenCalledWith('/api/cp/protection')
  })
})
