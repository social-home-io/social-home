import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import type { FeedPost } from '@/types'

export const posts        = signal<FeedPost[]>([])
export const feedLoading  = signal(false)
export const feedHasMore  = signal(true)

export async function loadFeed(before?: string) {
  feedLoading.value = true
  const data = await api.get(`/api/feed${before ? `?before=${before}` : ''}`)
  posts.value = before ? [...posts.value, ...data] : data
  feedHasMore.value = data.length === 50
  feedLoading.value = false
}

/** Wire post/comment WS events into the feed store. Idempotent. */
export function wireFeedWs(): void {
  ws.on('post.created', (e) => {
    const post = e.data as unknown as FeedPost
    if (!posts.value.some((p) => p.id === post.id)) {
      posts.value = [post, ...posts.value]
    }
  })
  ws.on('post.edited', (e) => {
    const post = e.data as unknown as FeedPost
    posts.value = posts.value.map((p) => (p.id === post.id ? post : p))
  })
  ws.on('post.deleted', (e) => {
    const id = (e.data as { id: string }).id
    posts.value = posts.value.filter((p) => p.id !== id)
  })
  ws.on('comment.added', (e) => {
    const { post_id } = e.data as unknown as { post_id: string }
    posts.value = posts.value.map((p) =>
      p.id === post_id ? { ...p, comment_count: p.comment_count + 1 } : p,
    )
  })
}
