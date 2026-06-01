/**
 * SpaceHero — the cover banner + About blurb shown at the top of a space.
 *
 * Rendered on the space feed when an admin has set a cover image and/or an
 * About markdown (Space → Settings → About). The cover is a 16:5 hero
 * banner with the space name + emoji overlaid; with no cover it falls back
 * to a primary→accent gradient (so an About-only space still gets a header).
 * The About markdown renders below the banner. Styling lives in
 * ``.sh-space-hero*`` (app.css).
 */
import { MarkdownView } from './MarkdownView'

interface Props {
  name: string
  emoji: string | null
  coverUrl: string | null
  about: string | null
}

export function SpaceHero({ name, emoji, coverUrl, about }: Props) {
  return (
    <div class="sh-space-hero">
      <div
        class={
          coverUrl
            ? 'sh-space-hero-banner'
            : 'sh-space-hero-banner sh-space-hero-banner--gradient'
        }
      >
        {coverUrl && (
          <img class="sh-space-hero-image" src={coverUrl} alt="" />
        )}
        <div class="sh-space-hero-overlay">
          {emoji && (
            <span class="sh-space-hero-emoji" aria-hidden="true">
              {emoji}
            </span>
          )}
          <h2 class="sh-space-hero-name">{name}</h2>
        </div>
      </div>
      {about && (
        <div class="sh-space-hero-about">
          <MarkdownView src={about} />
        </div>
      )}
    </div>
  )
}
