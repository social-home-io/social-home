import { describe, it, expect } from 'vitest'

describe('MemberActionSheet', () => {
  it('module exports exist', async () => {
    const mod = await import('./MemberActionSheet')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  it('openMemberActions accepts an instance_id for remote members (#114)', async () => {
    // Smoke check: the signature accepts the new optional argument
    // without TypeScript balking. The behaviour the argument drives
    // (routing PATCH/DELETE to /remote-members/{instance}/{user})
    // is exercised in tests/services/test_space_service_federation_coverage.py
    // and tests/federation/test_private_invite_handler.py on the
    // backend side; the SPA contract is just "thread the field through".
    const mod = await import('./MemberActionSheet')
    expect(typeof mod.openMemberActions).toBe('function')
    // Should not throw when called with the new signature shape.
    mod.openMemberActions('sp-1', 'u-bob', 'member', 'peer-instance-id')
  })
})
