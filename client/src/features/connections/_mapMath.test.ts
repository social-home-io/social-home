/**
 * Tests for the pure geographic helpers used by FederationMap peer popups.
 */
import { describe, test, expect } from 'vitest'
import { haversineKm, bearing8, roundKm } from './_mapMath'

describe('haversineKm', () => {
  test('Berlin → Hamburg ≈ 254 km', () => {
    const km = haversineKm(52.52, 13.40, 53.55, 9.99)
    expect(km).toBeGreaterThan(250)
    expect(km).toBeLessThan(260)
  })

  test('same point → 0 km', () => {
    expect(haversineKm(48.85, 2.35, 48.85, 2.35)).toBe(0)
  })

  test('equatorial degree ≈ 111 km', () => {
    const km = haversineKm(0, 0, 0, 1)
    expect(km).toBeGreaterThan(110)
    expect(km).toBeLessThan(112)
  })
})

describe('bearing8', () => {
  test('Berlin → Hamburg is NW', () => {
    expect(bearing8(52.52, 13.40, 53.55, 9.99)).toBe('NW')
  })

  test('Berlin → Frankfurt is SW', () => {
    expect(bearing8(52.52, 13.40, 50.11, 8.68)).toBe('SW')
  })

  test('due north', () => {
    expect(bearing8(0, 0, 1, 0)).toBe('N')
  })

  test('due east', () => {
    expect(bearing8(0, 0, 0, 1)).toBe('E')
  })

  test('due south', () => {
    expect(bearing8(1, 0, 0, 0)).toBe('S')
  })

  test('due west', () => {
    expect(bearing8(0, 1, 0, 0)).toBe('W')
  })
})

describe('roundKm', () => {
  test('< 100 km rounds to nearest 10', () => {
    expect(roundKm(58)).toBe(60)
    expect(roundKm(51)).toBe(50)
    expect(roundKm(99)).toBe(100)
    expect(roundKm(1)).toBe(0)
  })

  test('< 500 km rounds to nearest 50', () => {
    expect(roundKm(258)).toBe(250)
    expect(roundKm(275)).toBe(300)
    expect(roundKm(499)).toBe(500)
  })

  test('>= 500 km rounds to nearest 100', () => {
    expect(roundKm(1234)).toBe(1200)
    expect(roundKm(500)).toBe(500)
    expect(roundKm(850)).toBe(900)
  })
})
