/**
 * Skeleton — primitive shimmer-block + page-shaped helpers (§UX).
 *
 * Replaces the generic ``<Spinner />`` first-paint flash with a
 * placeholder that matches the final layout. The layout-stable
 * approach lets the eye relax on the page chrome (avatar circle,
 * post body bars, ring grid) while the data lands; when the real
 * content swaps in there's no jarring re-mount.
 *
 * The primitives are intentionally bare — width / height / shape are
 * controlled inline by callers because each page has its own
 * proportions. A single ``.sh-skeleton`` rule in ``app.css`` provides
 * the shared shimmer animation.
 */
import type { JSX } from 'preact'


interface SkeletonProps {
  /** ``rect`` (default): rounded rectangle. ``line``: short text bar.
   *  ``circle``: avatar / ring placeholder. ``card``: card-shaped
   *  block with the standard card chrome. */
  shape?: 'rect' | 'line' | 'circle' | 'card'
  width?: number | string
  height?: number | string
  /** Additional class names for layout / margins. */
  class?: string
  /** Override the ARIA label that screen readers announce while the
   *  skeleton is on screen. Default: "Loading". */
  ariaLabel?: string
  style?: JSX.CSSProperties
}


export function Skeleton({
  shape = 'rect',
  width,
  height,
  class: className,
  ariaLabel,
  style,
}: SkeletonProps) {
  const cls =
    `sh-skeleton sh-skeleton--${shape}` + (className ? ` ${className}` : '')
  const inline: JSX.CSSProperties = { ...style }
  if (width != null) inline.width = typeof width === 'number' ? `${width}px` : width
  if (height != null) inline.height = typeof height === 'number' ? `${height}px` : height
  return (
    <span
      class={cls}
      style={inline}
      role="status"
      aria-label={ariaLabel ?? 'Loading'}
      aria-busy="true"
    />
  )
}


/** Stand-in for a ``PostCard`` while the feed loads. Reproduces the
 *  card chrome (avatar, two-line author meta, body lines, optional
 *  media block) so the layout doesn't shift when the real card lands. */
export function PostCardSkeleton({ withMedia = false }: { withMedia?: boolean }) {
  return (
    <article class="sh-post sh-post--skeleton" aria-hidden="true">
      <div class="sh-post-header">
        <Skeleton shape="circle" width={40} height={40} />
        <div class="sh-post-meta">
          <Skeleton shape="line" width="35%" height={12} />
          <Skeleton shape="line" width="20%" height={10} class="sh-skeleton-spaced" />
        </div>
      </div>
      <div class="sh-post-body">
        <Skeleton shape="line" width="92%" height={12} />
        <Skeleton shape="line" width="78%" height={12} class="sh-skeleton-spaced" />
        <Skeleton shape="line" width="55%" height={12} class="sh-skeleton-spaced" />
        {withMedia && (
          <Skeleton
            shape="rect"
            width="100%"
            height={180}
            class="sh-skeleton-spaced"
          />
        )}
      </div>
    </article>
  )
}


/** Stand-in for the household feed: 3 post cards (one with media)
 *  plus a thin presence-strip header row. */
export function FeedSkeleton() {
  return (
    <div class="sh-feed sh-feed--skeleton" aria-busy="true">
      <div class="sh-skeleton-presence-strip">
        {[0, 1, 2, 3].map(i => (
          <Skeleton key={i} shape="circle" width={36} height={36} />
        ))}
      </div>
      <div class="sh-feed-item"><PostCardSkeleton /></div>
      <div class="sh-feed-item"><PostCardSkeleton withMedia /></div>
      <div class="sh-feed-item"><PostCardSkeleton /></div>
    </div>
  )
}


/** Stand-in for the DM inbox: 5 rows with avatar + name + preview. */
export function DmInboxSkeleton() {
  return (
    <ul class="sh-dm-inbox sh-dm-inbox--skeleton" aria-busy="true">
      {[0, 1, 2, 3, 4].map(i => (
        <li key={i} class="sh-dm-inbox-row">
          <Skeleton shape="circle" width={44} height={44} />
          <div class="sh-dm-inbox-row-meta">
            <Skeleton shape="line" width="40%" height={12} />
            <Skeleton shape="line" width="65%" height={10} class="sh-skeleton-spaced" />
          </div>
        </li>
      ))}
    </ul>
  )
}


/** Stand-in for the Stories inbox: + tile + 5 rings. */
export function StoriesRingSkeleton() {
  return (
    <div class="sh-stories-page sh-stories-page--skeleton" aria-busy="true">
      <header class="sh-stories-header">
        <Skeleton shape="line" width={120} height={20} />
      </header>
      <div class="sh-story-rings">
        {[0, 1, 2, 3, 4].map(i => (
          <div key={i} class="sh-story-ring">
            <Skeleton shape="circle" width={64} height={64} />
            <Skeleton shape="line" width={56} height={10} class="sh-skeleton-spaced" />
          </div>
        ))}
      </div>
    </div>
  )
}
