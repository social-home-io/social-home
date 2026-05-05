/**
 * Tests for renderHashtagged — keep the regex in sync with the
 * server-side extractor in ``socialhome/domain/moment.py``.
 */
import { describe, expect, it, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { renderHashtagged } from './hashtags'

describe('renderHashtagged', () => {
  it('passes through content with no hashtag', () => {
    const out = renderHashtagged('plain text', () => {})
    const { container } = render(<>{out}</>)
    expect(container.textContent).toBe('plain text')
    expect(container.querySelector('a')).toBeNull()
  })

  it('linkifies a hashtag and lowercases the slug', () => {
    const onClick = vi.fn()
    const out = renderHashtagged('Trip to #Berlin tomorrow', onClick)
    const { container } = render(<>{out}</>)
    const link = container.querySelector('a.sh-hashtag') as HTMLAnchorElement
    expect(link).not.toBeNull()
    expect(link.textContent).toBe('#Berlin')
    expect(link.getAttribute('href')).toBe('/momentum/archive?tag=berlin')
    fireEvent.click(link)
    expect(onClick).toHaveBeenCalledWith('berlin', expect.anything())
  })

  it('does not match mid-word "#"', () => {
    const out = renderHashtagged('issue#42 is unrelated', () => {})
    const { container } = render(<>{out}</>)
    expect(container.querySelector('a.sh-hashtag')).toBeNull()
    expect(container.textContent).toBe('issue#42 is unrelated')
  })

  it('renders multiple tags and preserves surrounding text', () => {
    const out = renderHashtagged('#one and #two', () => {})
    const { container } = render(<>{out}</>)
    const links = container.querySelectorAll('a.sh-hashtag')
    expect(links.length).toBe(2)
    expect(links[0].textContent).toBe('#one')
    expect(links[1].textContent).toBe('#two')
    expect(container.textContent).toBe('#one and #two')
  })
})
