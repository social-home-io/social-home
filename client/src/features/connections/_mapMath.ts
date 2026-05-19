/**
 * Pure geographic helpers for FederationMap peer popups.
 *
 * Extracted to a sibling module so they can be tested independently
 * of the Leaflet component.
 */

export function haversineKm(
  lat1: number, lon1: number, lat2: number, lon2: number,
): number {
  const R = 6371 // km
  const toRad = (d: number) => (d * Math.PI) / 180
  const dLat = toRad(lat2 - lat1)
  const dLon = toRad(lon2 - lon1)
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(a))
}

export function bearing8(
  lat1: number, lon1: number, lat2: number, lon2: number,
): 'N' | 'NE' | 'E' | 'SE' | 'S' | 'SW' | 'W' | 'NW' {
  const toRad = (d: number) => (d * Math.PI) / 180
  const φ1 = toRad(lat1)
  const φ2 = toRad(lat2)
  const Δλ = toRad(lon2 - lon1)
  const y = Math.sin(Δλ) * Math.cos(φ2)
  const x =
    Math.cos(φ1) * Math.sin(φ2) -
    Math.sin(φ1) * Math.cos(φ2) * Math.cos(Δλ)
  const deg = ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360
  // 8 buckets, centred on each cardinal/intercardinal at 0/45/90/...
  const dirs = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'] as const
  return dirs[Math.round(deg / 45) % 8]
}

export function roundKm(km: number): number {
  if (km < 100) return Math.round(km / 10) * 10
  if (km < 500) return Math.round(km / 50) * 50
  return Math.round(km / 100) * 100
}
