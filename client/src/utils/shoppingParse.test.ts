import { describe, it, expect } from 'vitest'
import { parseItemInput } from './shoppingParse'

describe('parseItemInput', () => {
  it('returns text + null store for a plain item', () => {
    expect(parseItemInput('Milk')).toEqual({ text: 'Milk', store: null })
  })

  it('splits "text @ store" on the @-suffix', () => {
    expect(parseItemInput('Milk @ Aldi')).toEqual({
      text: 'Milk',
      store: 'Aldi',
    })
  })

  it('trims surrounding whitespace around the @', () => {
    expect(parseItemInput('Milk  @  Aldi  ')).toEqual({
      text: 'Milk',
      store: 'Aldi',
    })
  })

  it('also works without spaces around @', () => {
    expect(parseItemInput('Milk@Aldi')).toEqual({
      text: 'Milk',
      store: 'Aldi',
    })
  })

  it('splits on the LAST @ so "Wine @ Whole Foods @ Main" picks the trailing store', () => {
    // Last @ wins — lets a store name carry an inner ``@`` if the
    // user wants ("Cafe @ The Square" reads naturally even though
    // there's an @ in the name).
    expect(parseItemInput('Wine @ Whole Foods @ Main')).toEqual({
      text: 'Wine @ Whole Foods',
      store: 'Main',
    })
  })

  it('collapses an empty store after the @ to null', () => {
    expect(parseItemInput('Milk @')).toEqual({ text: 'Milk', store: null })
    expect(parseItemInput('Milk @  ')).toEqual({ text: 'Milk', store: null })
  })

  it('returns empty text when the input is only "@ store"', () => {
    // Caller decides whether to skip empty-text — the parser stays
    // honest about what it received.
    expect(parseItemInput('@ Aldi')).toEqual({ text: '', store: 'Aldi' })
  })

  it('returns empty text + null store for a blank or whitespace string', () => {
    expect(parseItemInput('')).toEqual({ text: '', store: null })
    expect(parseItemInput('   ')).toEqual({ text: '', store: null })
  })

  it('tolerates null / undefined gracefully', () => {
    expect(parseItemInput(null as unknown as string)).toEqual({
      text: '',
      store: null,
    })
    expect(parseItemInput(undefined as unknown as string)).toEqual({
      text: '',
      store: null,
    })
  })
})
