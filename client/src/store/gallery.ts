/**
 * Gallery store — keeps the album + item lists in sync with WS events.
 *
 * Components that already render their own album/item lists may keep
 * local state; this store is the central cache used by NetworkMap-like
 * cross-cutting widgets (e.g. the activity feed). Either way, the
 * `wireGalleryWs()` hook should run once at app startup so the store
 * stays current as other family members add or delete media.
 */
import { signal } from '@preact/signals'
import { ws } from '@/ws'

export interface GalleryAlbum {
  id:            string
  space_id:      string | null
  owner_user_id: string
  name:          string
  cover_url:     string | null
  item_count:    number
  created_at:    string
}

export interface GalleryItem {
  id:        string
  album_id:  string
  url:       string
  thumbnail_url: string | null
  uploaded_by: string
  created_at: string
}

export const albums = signal<GalleryAlbum[]>([])
export const itemsByAlbum = signal<Record<string, GalleryItem[]>>({})

/** Bind gallery.* WS events to the local store. Idempotent. */
export function wireGalleryWs(): void {
  ws.on('gallery.album_created', (e) => {
    const data = e.data as unknown as GalleryAlbum
    if (!albums.value.some((a) => a.id === data.id)) {
      albums.value = [data, ...albums.value]
    }
  })
  ws.on('gallery.album_deleted', (e) => {
    const id = (e.data as { id: string }).id
    albums.value = albums.value.filter((a) => a.id !== id)
    const next = { ...itemsByAlbum.value }
    delete next[id]
    itemsByAlbum.value = next
  })
  ws.on('gallery.item_uploaded', (e) => {
    const data = e.data as unknown as GalleryItem
    const list = itemsByAlbum.value[data.album_id] ?? []
    if (!list.some((it) => it.id === data.id)) {
      itemsByAlbum.value = {
        ...itemsByAlbum.value,
        [data.album_id]: [data, ...list],
      }
    }
    // Bump the album's item_count if we know the album.
    albums.value = albums.value.map((a) =>
      a.id === data.album_id
        ? { ...a, item_count: a.item_count + 1 }
        : a,
    )
  })
  ws.on('gallery.item_deleted', (e) => {
    const { id, album_id } = e.data as unknown as { id: string; album_id: string }
    const list = itemsByAlbum.value[album_id] ?? []
    itemsByAlbum.value = {
      ...itemsByAlbum.value,
      [album_id]: list.filter((it) => it.id !== id),
    }
    albums.value = albums.value.map((a) =>
      a.id === album_id
        ? { ...a, item_count: Math.max(0, a.item_count - 1) }
        : a,
    )
  })
}
