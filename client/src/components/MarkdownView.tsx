/**
 * MarkdownView — sanitised Markdown rendering for the Pages feature.
 *
 * Thin wrapper around `renderMarkdown` that spits the result into a
 * `<div>` via `dangerouslySetInnerHTML`. The div gets `.sh-markdown`
 * so typographic rules in `app.css` apply (heading hierarchy, code
 * font, list indent, table borders, blockquote rail).
 *
 * Embedded ``<img>`` clicks are delegated to the global
 * :func:`openLightbox` overlay — same UX as the gallery, feed and
 * moments. The handler collects every image in the rendered body so
 * the lightbox prev/next walks through all of them, not just the
 * clicked one.
 */
import { openLightbox, type LightboxItem } from '@/components/ImageLightbox'
import { renderMarkdown } from '@/utils/markdown'

interface Props {
  src: string
  /** Override the outer class — defaults to `sh-markdown`. */
  class?: string
  /** When true, the container gets `aria-live="polite"` so screen
   * readers announce content changes (used by the live-preview pane).*/
  live?: boolean
}

function onMarkdownClick(ev: MouseEvent) {
  const target = ev.target as HTMLElement | null
  if (!target || target.tagName !== 'IMG') return
  // Honour markdown image links — ``[![alt](src)](href)`` should
  // navigate, not zoom. Walk up looking for an anchor before the
  // wrapper.
  let node: HTMLElement | null = target
  while (node && node !== ev.currentTarget) {
    if (node.tagName === 'A') return
    node = node.parentElement
  }
  const wrapper = ev.currentTarget as HTMLElement
  const imgs = Array.from(wrapper.querySelectorAll('img'))
  const index = imgs.indexOf(target as HTMLImageElement)
  if (index < 0) return
  ev.preventDefault()
  const items: LightboxItem[] = imgs.map((img) => ({
    url:       img.currentSrc || img.src,
    item_type: 'photo',
    caption:   img.getAttribute('alt') || img.getAttribute('title') || null,
  }))
  openLightbox({ items, index })
}

export function MarkdownView({ src, class: klass, live }: Props) {
  const html = renderMarkdown(src || '')
  return (
    <div
      class={klass || 'sh-markdown'}
      aria-live={live ? 'polite' : undefined}
      onClick={onMarkdownClick}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}
