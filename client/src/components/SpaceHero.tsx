/**
 * SpaceHero — the branded profile-style header at the top of a space
 * (Twitter / Facebook cover pattern).
 *
 * A wide cover banner (the admin's uploaded image, or a primary→accent
 * theme gradient when none is set) with the space's icon as a circular
 * avatar overlapping the banner's bottom-left, the name + member count
 * beside it, and the About markdown below. Shown on the space feed when
 * the admin has branded the space (cover and/or About). Styling lives in
 * ``.sh-space-hero*`` (app.css); the theme colours flow in via the
 * per-space CSS vars set by ``useSpaceTheme``.
 */
import { MarkdownView } from './MarkdownView'

interface Props {
  name: string
  emoji: string | null
  coverUrl: string | null
  about: string | null
  memberCount?: number | null
}

export function SpaceHero({ name, emoji, coverUrl, about, memberCount }: Props) {
  const icon = emoji || name.trim().charAt(0).toUpperCase() || '🏠'
  return (
    <header class="sh-space-hero">
      <div
        class={
          coverUrl
            ? 'sh-space-hero-banner'
            : 'sh-space-hero-banner sh-space-hero-banner--gradient'
        }
      >
        {coverUrl && <img class="sh-space-hero-image" src={coverUrl} alt="" />}
      </div>
      <div class="sh-space-hero-identity">
        <span class="sh-space-hero-avatar" aria-hidden="true">
          {icon}
        </span>
        <div class="sh-space-hero-meta">
          <h2 class="sh-space-hero-name">{name}</h2>
          {memberCount != null && (
            <span class="sh-space-hero-members">
              {memberCount} {memberCount === 1 ? 'member' : 'members'}
            </span>
          )}
        </div>
      </div>
      {about && (
        <div class="sh-space-hero-about">
          <MarkdownView src={about} />
        </div>
      )}
    </header>
  )
}
