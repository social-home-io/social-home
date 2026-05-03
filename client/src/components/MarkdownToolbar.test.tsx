import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

import { MarkdownToolbar } from './MarkdownToolbar'

function makeRef() {
  const ta = document.createElement('textarea')
  ta.value = ''
  return { current: ta }
}

describe('MarkdownToolbar', () => {
  it('module exports exist', async () => {
    const mod = await import('./MarkdownToolbar')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  it('does not render the image button when onPickImage is omitted', () => {
    const { queryByRole } = render(
      <MarkdownToolbar textareaRef={makeRef()} onUpdate={() => {}} />,
    )
    expect(queryByRole('button', { name: /image/i })).toBeNull()
  })

  it('renders the image button and invokes onPickImage when wired', () => {
    const onPickImage = vi.fn()
    const { getByRole } = render(
      <MarkdownToolbar
        textareaRef={makeRef()}
        onUpdate={() => {}}
        onPickImage={onPickImage}
      />,
    )
    const btn = getByRole('button', { name: /image/i })
    fireEvent.click(btn)
    expect(onPickImage).toHaveBeenCalledOnce()
  })
})
