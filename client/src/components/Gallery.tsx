/**
 * Gallery — image grid + album view (§23.119).
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { openLightbox } from './ImageLightbox'
import { Spinner } from './Spinner'

interface GalleryItem { id: string; thumbnail_url: string; full_url: string; caption?: string }
interface Album { id: string; name: string; item_count: number; cover_url?: string }

const albums = signal<Album[]>([])
const items = signal<GalleryItem[]>([])
const activeAlbum = signal<string | null>(null)
const loading = signal(true)

export function Gallery({ spaceId }: { spaceId?: string }) {
  useEffect(() => {
    // v1: gallery data comes from space posts with media
    loading.value = false
    albums.value = []
    items.value = []
  }, [spaceId])

  if (loading.value) return <Spinner />

  return (
    <div class="sh-gallery">
      <h3>Gallery</h3>
      {items.value.length === 0 && albums.value.length === 0 && (
        <p class="sh-muted">No images yet. Upload photos in posts to see them here.</p>
      )}
      {albums.value.length > 0 && (
        <div class="sh-album-grid">
          {albums.value.map(a => (
            <div key={a.id} class="sh-album-card" onClick={() => activeAlbum.value = a.id}>
              {a.cover_url && (
                <img
                  src={a.cover_url}
                  class="sh-album-cover"
                  loading="lazy"
                  alt={a.name}
                />
              )}
              <div class="sh-album-info">
                <strong>{a.name}</strong>
                <span class="sh-muted">{a.item_count} items</span>
              </div>
            </div>
          ))}
        </div>
      )}
      <div class="sh-image-grid">
        {items.value.map(item => (
          <div key={item.id} class="sh-gallery-item"
            onClick={() => openLightbox(item.full_url)}>
            <img src={item.thumbnail_url} alt={item.caption || ''} loading="lazy" />
          </div>
        ))}
      </div>
    </div>
  )
}
