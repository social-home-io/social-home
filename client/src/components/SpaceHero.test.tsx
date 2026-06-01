import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { SpaceHero } from './SpaceHero'

describe('SpaceHero', () => {
  it('renders the cover image + name + about markdown when set', () => {
    const { container } = render(
      <SpaceHero
        name="Family Hub"
        emoji="🏡"
        coverUrl="/media/cover.webp"
        about="# Welcome\n\nOur **family** space."
      />,
    )
    const img = container.querySelector('.sh-space-hero-image') as HTMLImageElement
    expect(img).toBeTruthy()
    expect(img.getAttribute('src')).toBe('/media/cover.webp')
    expect(container.querySelector('.sh-space-hero-name')?.textContent).toBe(
      'Family Hub',
    )
    expect(container.querySelector('.sh-space-hero-about')).toBeTruthy()
    expect(container.textContent).toContain('Welcome')
  })

  it('falls back to a gradient banner (no image) when there is no cover', () => {
    const { container } = render(
      <SpaceHero name="No Cover" emoji={null} coverUrl={null} about="Just text" />,
    )
    expect(container.querySelector('.sh-space-hero-image')).toBeNull()
    expect(
      container.querySelector('.sh-space-hero-banner--gradient'),
    ).toBeTruthy()
    expect(container.querySelector('.sh-space-hero-about')).toBeTruthy()
  })

  it('shows the emoji as the avatar + the member count', () => {
    const { container } = render(
      <SpaceHero
        name="Family Hub"
        emoji="🏡"
        coverUrl={null}
        about={null}
        memberCount={3}
      />,
    )
    expect(container.querySelector('.sh-space-hero-avatar')?.textContent).toBe('🏡')
    expect(container.querySelector('.sh-space-hero-members')?.textContent).toContain(
      '3 members',
    )
  })

  it('falls back to the name initial when there is no emoji, and singular member', () => {
    const { container } = render(
      <SpaceHero name="zeta" emoji={null} coverUrl={null} about={null} memberCount={1} />,
    )
    expect(container.querySelector('.sh-space-hero-avatar')?.textContent).toBe('Z')
    expect(container.querySelector('.sh-space-hero-members')?.textContent).toContain(
      '1 member',
    )
  })

  it('omits the about block when there is no about text', () => {
    const { container } = render(
      <SpaceHero name="Cover only" emoji="🎉" coverUrl="/x.webp" about={null} />,
    )
    expect(container.querySelector('.sh-space-hero-about')).toBeNull()
    expect(container.querySelector('.sh-space-hero-image')).toBeTruthy()
  })
})
