/**
 * SearchPage — full-text search over posts / spaces / people / pages /
 * messages (spec §23.2).
 *
 * Hits ``GET /api/search?q=&type=&space_id=&limit=`` and renders the
 * snippet (which the backend wraps in ``<mark>`` tags via FTS5
 * ``snippet()``).
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'

interface Hit {
  scope: string
  ref_id: string
  space_id: string | null
  title: string
  snippet: string
}

interface SearchResponse {
  hits: Hit[]
  counts: Record<string, number>
}

const query = signal('')
/** spec's high-level filter ``type``. '' means All (no filter). */
const activeType = signal<string>('')
const hits = signal<Hit[]>([])
const counts = signal<Record<string, number>>({})
const loading = signal(false)
const loadingMore = signal(false)
const lastQuery = signal('')
/** Page size — mirrors the server default so "load more" returns a
 *  full batch unless the caller is at the end of results. */
const PAGE_SIZE = 20
/** True while the last request returned a full page (so there may be
 *  more). Reset on each fresh query. */
const hasMore = signal(false)

/** Filter chips per spec §23.2.3. */
const FILTERS: { type: string, label: string }[] = [
  { type: '',         label: 'All'    },
  { type: 'posts',    label: 'Posts'  },
  { type: 'people',   label: 'People' },
  { type: 'spaces',   label: 'Spaces' },
  { type: 'pages',    label: 'Pages'  },
  { type: 'messages', label: 'DMs'    },
]

const SCOPE_LABELS: Record<string, string> = {
  post:       'Household feed',
  space_post: 'Space post',
  message:    'Direct message',
  page:       'Page',
  user:       'Person',
  space:      'Space',
}

/** Sum of the underlying scopes each filter covers (for chip counts). */
function countsFor(type: string, all: Record<string, number>): number {
  if (type === '')         return Object.values(all).reduce((a, b) => a + b, 0)
  if (type === 'posts')    return (all.post ?? 0) + (all.space_post ?? 0)
  if (type === 'people')   return all.user ?? 0
  if (type === 'spaces')   return all.space ?? 0
  if (type === 'pages')    return all.page ?? 0
  if (type === 'messages') return all.message ?? 0
  return 0
}

function emptyMessage(type: string, q: string): string {
  switch (type) {
    case 'posts':    return `No posts matching "${q}"`
    case 'people':   return `No people matching "${q}"`
    case 'spaces':   return `No spaces matching "${q}"`
    case 'pages':    return `No pages matching "${q}"`
    case 'messages': return `No messages matching "${q}"`
    default:         return `No results for "${q}"`
  }
}

export default function SearchPage() {
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const q = params.get('q') || ''
    const t = params.get('type') || ''
    if (q) {
      query.value = q
      activeType.value = t
      void runSearch()
    }
  }, [])

  const q = query.value.trim()
  const tooShort = q.length > 0 && q.length < 2

  return (
    <div class="sh-search-page">
      <h2>Search</h2>
      <form
        onSubmit={(e) => { e.preventDefault(); void runSearch() }}
        class="sh-row"
      >
        <input
          type="search"
          placeholder="Search posts, people, spaces, pages, DMs…"
          value={query.value}
          onInput={(e) => (query.value = (e.target as HTMLInputElement).value)}
          autoFocus
        />
        <Button type="submit">Search</Button>
      </form>

      <div class="sh-filter-chips" role="tablist">
        {FILTERS.map(f => {
          const c = countsFor(f.type, counts.value)
          const isActive = activeType.value === f.type
          return (
            <button
              key={f.type}
              type="button"
              role="tab"
              aria-selected={isActive}
              class={`sh-chip ${isActive ? 'sh-chip-active' : ''}`}
              onClick={() => {
                activeType.value = f.type
                void runSearch()
              }}
            >
              {f.label}{c > 0 && <span class="sh-chip-count">{c}</span>}
            </button>
          )
        })}
      </div>

      {loading.value && <Spinner />}

      {!loading.value && tooShort && (
        <p class="sh-muted">Keep typing…</p>
      )}

      {!loading.value && !lastQuery.value && (
        <div class="sh-empty-state">
          <div aria-hidden="true">🔎</div>
          <h3>Find anything fast</h3>
          <p>Search across posts, people, spaces, pages, and direct messages.
             Use the filters above to narrow a search.</p>
        </div>
      )}

      {!loading.value && !tooShort && lastQuery.value && hits.value.length === 0 && (
        <div class="sh-empty-state">
          <div aria-hidden="true">🗂️</div>
          <h3>{emptyMessage(activeType.value, lastQuery.value)}</h3>
          <p>Try a different word, remove the filter, or check the spelling.</p>
        </div>
      )}

      <ul class="sh-search-results">
        {hits.value.map(h => (
          <li key={`${h.scope}:${h.ref_id}`} class="sh-search-hit sh-card">
            <header class="sh-row sh-justify-between">
              <strong>{SCOPE_LABELS[h.scope] || h.scope}</strong>
              {h.space_id && <span class="sh-muted">{h.space_id}</span>}
            </header>
            {h.title && <h4>{h.title}</h4>}
            <p
              class="sh-snippet"
              // FTS5 returns plain text wrapped in <mark> — we trust it
              // because both delimiters are server-controlled.
              dangerouslySetInnerHTML={{ __html: h.snippet }}
            />
          </li>
        ))}
      </ul>

      {hasMore.value && hits.value.length > 0 && (
        <div class="sh-row sh-justify-center">
          <Button onClick={() => void loadMore()} loading={loadingMore.value}>
            Load more
          </Button>
        </div>
      )}
    </div>
  )
}

async function runSearch() {
  const q = query.value.trim()
  if (q.length < 2) {
    hits.value = []
    counts.value = {}
    hasMore.value = false
    lastQuery.value = q
    return
  }
  loading.value = true
  lastQuery.value = q
  try {
    const params = new URLSearchParams({ q, limit: String(PAGE_SIZE) })
    if (activeType.value) params.set('type', activeType.value)
    const body = await api.get(`/api/search?${params.toString()}`) as SearchResponse
    hits.value = body.hits
    counts.value = body.counts ?? {}
    hasMore.value = body.hits.length === PAGE_SIZE
  } catch (err: unknown) {
    showToast(`Search failed: ${(err as Error)?.message ?? err}`, 'error')
    hits.value = []
    counts.value = {}
    hasMore.value = false
  } finally {
    loading.value = false
  }
}

async function loadMore() {
  const q = lastQuery.value
  if (q.length < 2 || !hasMore.value) return
  loadingMore.value = true
  try {
    const params = new URLSearchParams({
      q,
      limit:  String(PAGE_SIZE),
      offset: String(hits.value.length),
    })
    if (activeType.value) params.set('type', activeType.value)
    const body = await api.get(`/api/search?${params.toString()}`) as SearchResponse
    hits.value = [...hits.value, ...body.hits]
    hasMore.value = body.hits.length === PAGE_SIZE
  } catch (err: unknown) {
    showToast(`Search failed: ${(err as Error)?.message ?? err}`, 'error')
  } finally {
    loadingMore.value = false
  }
}
