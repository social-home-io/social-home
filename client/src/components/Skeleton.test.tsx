import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'

import {
  Skeleton,
  PostCardSkeleton,
  FeedSkeleton,
  DmInboxSkeleton,
  StoriesRingSkeleton,
  CalendarSkeleton,
  SpaceListSkeleton,
  BazaarSkeleton,
  TasksSkeleton,
} from './Skeleton'

describe('Skeleton primitive', () => {
  it('renders with the default rect shape and aria-busy', () => {
    const { container } = render(<Skeleton />)
    const el = container.querySelector('.sh-skeleton') as HTMLElement
    expect(el).toBeTruthy()
    expect(el.classList.contains('sh-skeleton--rect')).toBe(true)
    expect(el.getAttribute('aria-busy')).toBe('true')
  })

  it('honours the shape prop', () => {
    const { container } = render(<Skeleton shape="circle" />)
    expect(container.querySelector('.sh-skeleton--circle')).toBeTruthy()
  })

  it('applies the inline width / height as pixel strings', () => {
    const { container } = render(<Skeleton width={80} height={20} />)
    const el = container.querySelector('.sh-skeleton') as HTMLElement
    expect(el.style.width).toBe('80px')
    expect(el.style.height).toBe('20px')
  })

  it('passes through string width values verbatim (e.g. "40%")', () => {
    const { container } = render(<Skeleton width="40%" />)
    const el = container.querySelector('.sh-skeleton') as HTMLElement
    expect(el.style.width).toBe('40%')
  })
})

describe('Page-shaped skeletons', () => {
  it('PostCardSkeleton renders a header + body', () => {
    const { container } = render(<PostCardSkeleton />)
    expect(container.querySelector('.sh-post--skeleton')).toBeTruthy()
    expect(container.querySelectorAll('.sh-skeleton').length).toBeGreaterThan(3)
  })

  it('PostCardSkeleton renders a media block when withMedia', () => {
    const { container: a } = render(<PostCardSkeleton />)
    const { container: b } = render(<PostCardSkeleton withMedia />)
    // The media-bearing variant has at least one more skeleton block
    // than the bare variant.
    expect(b.querySelectorAll('.sh-skeleton').length)
      .toBeGreaterThan(a.querySelectorAll('.sh-skeleton').length)
  })

  it('FeedSkeleton renders 3 post-shaped skeletons', () => {
    const { container } = render(<FeedSkeleton />)
    expect(container.querySelectorAll('.sh-post--skeleton').length).toBe(3)
  })

  it('DmInboxSkeleton renders 5 inbox rows', () => {
    const { container } = render(<DmInboxSkeleton />)
    expect(container.querySelectorAll('.sh-dm-inbox-row').length).toBe(5)
  })

  it('StoriesRingSkeleton renders 5 ring placeholders', () => {
    const { container } = render(<StoriesRingSkeleton />)
    expect(container.querySelectorAll('.sh-story-ring').length).toBe(5)
  })

  it('CalendarSkeleton renders a 35-cell month grid', () => {
    const { container } = render(<CalendarSkeleton />)
    expect(
      container.querySelectorAll('.sh-skeleton-calendar-day').length,
    ).toBe(35)
  })

  it('SpaceListSkeleton defaults to 4 cards', () => {
    const { container } = render(<SpaceListSkeleton />)
    expect(
      container.querySelectorAll('.sh-space-card--skeleton').length,
    ).toBe(4)
  })

  it('SpaceListSkeleton honours the count prop', () => {
    const { container } = render(<SpaceListSkeleton count={6} />)
    expect(
      container.querySelectorAll('.sh-space-card--skeleton').length,
    ).toBe(6)
  })

  it('BazaarSkeleton renders 3 listing-card placeholders', () => {
    const { container } = render(<BazaarSkeleton />)
    expect(
      container.querySelectorAll('.sh-bazaar-card--skeleton').length,
    ).toBe(3)
  })

  it('TasksSkeleton renders sidebar + 5 task rows', () => {
    const { container } = render(<TasksSkeleton />)
    expect(container.querySelector('.sh-tasks-sidebar')).toBeTruthy()
    expect(container.querySelectorAll('.sh-skeleton-task-row').length).toBe(5)
  })
})
