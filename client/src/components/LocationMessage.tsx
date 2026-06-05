/**
 * LocationMessage — location sharing in DMs (§23.131).
 */
import { useSignal } from '@preact/signals'
import { Button } from './Button'
import { showToast } from './Toast'

export function LocationMessage({ lat, lon, label }: {
  lat: number; lon: number; label?: string
}) {
  // Render a static map tile (no API key needed with OSM tiles)
  const mapUrl = `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=15/${lat}/${lon}`
  return (
    <a href={mapUrl} target="_blank" rel="noopener noreferrer" class="sh-location-msg">
      <div class="sh-location-pin">📍</div>
      <div class="sh-location-info">
        <strong>{label || 'Shared location'}</strong>
        <span class="sh-muted">{lat.toFixed(4)}, {lon.toFixed(4)}</span>
      </div>
    </a>
  )
}

export function ShareLocationButton({ onShare }: {
  onShare: (lat: number, lon: number) => void
}) {
  const sharing = useSignal(false)

  const share = () => {
    if (!navigator.geolocation) {
      showToast('Geolocation not available', 'error')
      return
    }
    sharing.value = true
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        onShare(pos.coords.latitude, pos.coords.longitude)
        sharing.value = false
      },
      () => { showToast('Could not get location', 'error'); sharing.value = false },
      { enableHighAccuracy: true, timeout: 10000 },
    )
  }

  return (
    <Button variant="secondary" onClick={share} loading={sharing.value}>
      📍 Share location
    </Button>
  )
}
