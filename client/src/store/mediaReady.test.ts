/**
 * Tests for the mediaReady store — filename extraction, ready-set
 * mutation, and the test reset helper.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import {
  readyMedia,
  markMediaReady,
  mediaFilename,
  _resetMediaReadyForTest,
} from './mediaReady'

describe('mediaFilename', () => {
  it('strips the query string', () => {
    expect(mediaFilename('api/media/abc.webm?exp=1&sig=xyz')).toBe('abc.webm')
  })
  it('strips the path, keeping the last segment', () => {
    expect(mediaFilename('/api/media/abc.webm')).toBe('abc.webm')
  })
  it('handles a bare filename', () => {
    expect(mediaFilename('abc.webm')).toBe('abc.webm')
  })
  it('returns empty string for null / undefined / empty', () => {
    expect(mediaFilename(null)).toBe('')
    expect(mediaFilename(undefined)).toBe('')
    expect(mediaFilename('')).toBe('')
  })
})

describe('markMediaReady', () => {
  beforeEach(() => { _resetMediaReadyForTest() })

  it('adds a filename to the ready set', () => {
    markMediaReady('abc.webm')
    expect(readyMedia.value.has('abc.webm')).toBe(true)
  })

  it('is idempotent and keeps the signal reference stable on a no-op', () => {
    markMediaReady('abc.webm')
    const ref = readyMedia.value
    markMediaReady('abc.webm')
    expect(readyMedia.value).toBe(ref)
  })

  it('ignores an empty filename', () => {
    markMediaReady('')
    expect(readyMedia.value.size).toBe(0)
  })
})

describe('_resetMediaReadyForTest', () => {
  it('clears the ready set', () => {
    markMediaReady('abc.webm')
    _resetMediaReadyForTest()
    expect(readyMedia.value.size).toBe(0)
  })
})
