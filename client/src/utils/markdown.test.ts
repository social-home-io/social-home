import { describe, it, expect } from 'vitest'
import { renderMarkdown, extractHeadings } from './markdown'

describe('renderMarkdown', () => {
  it('renders bold + italic', () => {
    const html = renderMarkdown('**bold** _em_')
    expect(html).toContain('<strong>bold</strong>')
    expect(html).toContain('<em>em</em>')
  })

  it('renders GFM tables', () => {
    const html = renderMarkdown('| a | b |\n|---|---|\n| 1 | 2 |')
    expect(html).toContain('<table>')
    expect(html).toContain('<th>a</th>')
  })

  it('renders task lists (GFM)', () => {
    const html = renderMarkdown('- [x] done\n- [ ] todo')
    expect(html).toContain('<input')
    expect(html).toContain('checked')
    expect(html).toContain('disabled')
  })

  it('strips <script> and javascript: URLs', () => {
    const html = renderMarkdown(
      '<script>alert(1)</script>\n[x](javascript:alert(1))',
    )
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('javascript:')
  })

  it('strips inline event handlers', () => {
    const html = renderMarkdown('<img src="x" onerror="alert(1)" />')
    expect(html).not.toContain('onerror')
  })

  it('keeps safe http(s) links + adds mailto', () => {
    const html = renderMarkdown(
      '[site](https://example.com) [mail](mailto:a@b.co)',
    )
    expect(html).toContain('href="https://example.com"')
    expect(html).toContain('href="mailto:a@b.co"')
  })

  it('rewrites [[Wikilinks]] to /pages?title=...', () => {
    const html = renderMarkdown('See [[Other Page]] for more.')
    expect(html).toContain('href="/pages?title=Other%20Page"')
    expect(html).toContain('>Other Page</a>')
  })

  it('escapes raw HTML it does not recognise', () => {
    const html = renderMarkdown('Hello <iframe src="http://evil"></iframe>')
    expect(html).not.toContain('<iframe')
  })

  it('strips the leading slash from /api/ image sources for ingress', () => {
    // Server-rendered Page markdown can carry ``![](/api/media/<token>)``;
    // an absolute path bypasses ``<base href>``, which under HA Supervisor
    // ingress would 404 against HA Core's origin. The DOMPurify hook
    // rewrites the slash off so the URL resolves relative to the document
    // base — see ``markdown.ts``.
    const html = renderMarkdown('![](/api/media/abc?token=x)')
    expect(html).toContain('src="api/media/abc?token=x"')
    expect(html).not.toContain('src="/api/media/')
  })

  it('leaves non-/api absolute URLs untouched', () => {
    const html = renderMarkdown('[x](/feed)')
    // ``/feed`` is a local nav link; the IngressLocationProvider's click
    // interceptor handles the prefix at navigation time. Only ``/api/``
    // bodies need the URL surgery (they hit ``fetch``, not the router).
    expect(html).toContain('href="/feed"')
  })
})

describe('extractHeadings', () => {
  it('collects ## and ### with slugs', () => {
    const src = '# H1\n## Section A\n### Sub\n## Section B'
    const out = extractHeadings(src)
    expect(out).toEqual([
      { depth: 2, text: 'Section A', slug: 'section-a' },
      { depth: 3, text: 'Sub',       slug: 'sub' },
      { depth: 2, text: 'Section B', slug: 'section-b' },
    ])
  })

  it('returns [] on empty input', () => {
    expect(extractHeadings('')).toEqual([])
  })
})
