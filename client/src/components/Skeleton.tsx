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


/** Stand-in for ``CalendarPage``: header row + 5×7 day-grid placeholder
 *  shaped to match the rendered calendar so the swap is in-place. */
export function CalendarSkeleton() {
  return (
    <div class="sh-calendar sh-calendar--skeleton" aria-busy="true">
      <div class="sh-page-header">
        <Skeleton shape="line" width={140} height={22} />
        <Skeleton shape="rect" width={120} height={32} />
      </div>
      <div class="sh-calendar-controls">
        <Skeleton shape="rect" width={80} height={28} />
        <Skeleton shape="line" width={160} height={18} />
        <Skeleton shape="rect" width={80} height={28} />
      </div>
      <div class="sh-skeleton-calendar-grid" aria-hidden="true">
        {Array.from({ length: 35 }).map((_, i) => (
          <Skeleton
            key={i}
            shape="rect"
            width="100%"
            height={64}
            class="sh-skeleton-calendar-day"
          />
        ))}
      </div>
    </div>
  )
}


/** Stand-in for ``SpaceListPage`` and ``SpaceBrowserPage``: N space-card
 *  shapes with emoji + name + description bars. */
export function SpaceListSkeleton({ count = 4 }: { count?: number } = {}) {
  return (
    <div class="sh-spaces sh-spaces--skeleton" aria-busy="true">
      <div class="sh-page-header">
        <Skeleton shape="line" width={120} height={22} />
      </div>
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} class="sh-space-card sh-space-card--skeleton">
          <Skeleton shape="circle" width={44} height={44} />
          <div class="sh-space-card__body">
            <Skeleton shape="line" width="50%" height={14} />
            <Skeleton
              shape="line"
              width="80%"
              height={11}
              class="sh-skeleton-spaced"
            />
          </div>
        </div>
      ))}
    </div>
  )
}


/** Stand-in for ``BazaarPage``: 3 listing cards in a column. */
export function BazaarSkeleton() {
  return (
    <div class="sh-bazaar sh-bazaar--skeleton" aria-busy="true">
      <div class="sh-page-header">
        <Skeleton shape="line" width={120} height={22} />
        <Skeleton shape="rect" width={140} height={32} />
      </div>
      <div class="sh-bazaar-filters">
        <Skeleton shape="rect" width={100} height={28} />
        <Skeleton shape="rect" width={100} height={28} />
        <Skeleton shape="rect" width={140} height={28} />
      </div>
      {[0, 1, 2].map(i => (
        <div key={i} class="sh-bazaar-card sh-bazaar-card--skeleton">
          <Skeleton shape="rect" width={120} height={120} />
          <div class="sh-bazaar-card__body">
            <Skeleton shape="line" width="60%" height={14} />
            <Skeleton shape="line" width="35%" height={12} class="sh-skeleton-spaced" />
            <Skeleton shape="line" width="85%" height={11} class="sh-skeleton-spaced" />
            <Skeleton shape="line" width="70%" height={11} class="sh-skeleton-spaced" />
          </div>
        </div>
      ))}
    </div>
  )
}


/** Stand-in for ``TaskPage``: sidebar list + main column with task rows. */
export function TasksSkeleton() {
  return (
    <div class="sh-tasks sh-tasks--skeleton" aria-busy="true">
      <aside class="sh-tasks-sidebar">
        <Skeleton shape="line" width="60%" height={14} />
        {[0, 1, 2].map(i => (
          <Skeleton
            key={i}
            shape="line"
            width="80%"
            height={12}
            class="sh-skeleton-spaced"
          />
        ))}
      </aside>
      <main class="sh-tasks-main">
        <Skeleton shape="line" width="40%" height={20} />
        {[0, 1, 2, 3, 4].map(i => (
          <div key={i} class="sh-skeleton-task-row">
            <Skeleton shape="rect" width={18} height={18} />
            <Skeleton shape="line" width="65%" height={13} />
          </div>
        ))}
      </main>
    </div>
  )
}
