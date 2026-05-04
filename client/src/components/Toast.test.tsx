import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'

import { toasts, showToast, ToastContainer } from './Toast'

describe('Toast', () => {
  beforeEach(() => {
    toasts.value = []
  })

  it('showToast adds a toast to the list', () => {
    showToast('Test message', 'info')
    expect(toasts.value.length).toBe(1)
    expect(toasts.value[0].message).toBe('Test message')
    expect(toasts.value[0].count).toBe(1)
  })

  it('showToast auto-removes after timeout', () => {
    vi.useFakeTimers()
    try {
      showToast('Temp', 'success')
      expect(toasts.value.length).toBe(1)
      vi.advanceTimersByTime(5000)
      expect(toasts.value.length).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('collapses identical toasts into a count instead of stacking rows', () => {
    showToast('Saved', 'success')
    showToast('Saved', 'success')
    showToast('Saved', 'success')
    expect(toasts.value.length).toBe(1)
    expect(toasts.value[0].count).toBe(3)
  })

  it('caps the visible stack at 3 distinct toasts', () => {
    showToast('a', 'info')
    showToast('b', 'info')
    showToast('c', 'info')
    showToast('d', 'info')
    expect(toasts.value.length).toBe(3)
    // Oldest evicted; the most-recent three remain in order.
    expect(toasts.value.map(t => t.message)).toEqual(['b', 'c', 'd'])
  })

  it('treats different types as separate stacks even with the same message', () => {
    showToast('Saved', 'success')
    showToast('Saved', 'error')
    expect(toasts.value.length).toBe(2)
    expect(toasts.value.every(t => t.count === 1)).toBe(true)
  })

  it('renders a "× N" chip when count > 1, hides it for single rows', () => {
    showToast('Hello', 'info')
    const single = render(<ToastContainer />)
    expect(single.container.querySelector('.sh-toast-count')).toBeNull()
    single.unmount()

    showToast('Hello', 'info')
    showToast('Hello', 'info')
    const dup = render(<ToastContainer />)
    const chip = dup.container.querySelector('.sh-toast-count')
    expect(chip).not.toBeNull()
    expect(chip?.textContent).toContain('3')
  })
})
