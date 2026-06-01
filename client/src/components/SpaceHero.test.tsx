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

  it('omits the about block when there is no about text', () => {
    const { container } = render(
      <SpaceHero name="Cover only" emoji="🎉" coverUrl="/x.webp" about={null} />,
    )
    expect(container.querySelector('.sh-space-hero-about')).toBeNull()
    expect(container.querySelector('.sh-space-hero-image')).toBeTruthy()
  })
})
