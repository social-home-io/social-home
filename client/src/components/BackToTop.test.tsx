import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, act } from '@testing-library/preact'
import { BackToTop } from './BackToTop'

describe('BackToTop', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'scrollY', {
      value: 0, writable: true, configurable: true,
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('does not render when the page is at the top', () => {
    const { container } = render(<BackToTop />)
    expect(container.querySelector('.sh-back-to-top')).toBeNull()
  })

  it('renders past the threshold and scrolls to top on click', () => {
    const scrollSpy = vi.fn()
    Object.defineProperty(window, 'scrollTo', { value: scrollSpy, configurable: true })

    const { container } = render(<BackToTop />)
    // Simulate scrolling past the threshold (700 > 600).
    act(() => {
      Object.defineProperty(window, 'scrollY', {
        value: 700, writable: true, configurable: true,
      })
      window.dispatchEvent(new Event('scroll'))
    })
    const btn = container.querySelector('.sh-back-to-top') as HTMLButtonElement
    expect(btn).toBeTruthy()
    fireEvent.click(btn)
    expect(scrollSpy).toHaveBeenCalledOnce()
    const arg = scrollSpy.mock.calls[0][0] as ScrollToOptions
    expect(arg.top).toBe(0)
  })
})
