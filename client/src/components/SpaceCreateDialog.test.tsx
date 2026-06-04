import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, act, waitFor, cleanup } from '@testing-library/preact'

const post = vi.fn()
vi.mock('@/api', () => ({ api: { post: (...a: unknown[]) => post(...a) } }))
vi.mock('@/store/spaces', () => ({ loadSpaces: vi.fn() }))
vi.mock('./Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/i18n/i18n', () => ({ t: (k: string) => k }))
// EmojiField pulls in the emoji-picker; not relevant here.
vi.mock('./EmojiField', () => ({ EmojiField: () => null }))

const { SpaceCreateDialog, openSpaceCreate } = await import('./SpaceCreateDialog')

const nameInput = (c: Element) =>
  c.querySelector('input[placeholder="e.g. Family, Makers Club"]') as HTMLInputElement
const createBtn = (c: Element) =>
  [...c.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Create') as HTMLButtonElement
const pickVisibility = (c: Element, v: string) =>
  c.querySelector(`input[name="space-create-visibility"][value="${v}"]`) as HTMLInputElement

async function open() {
  const r = render(<SpaceCreateDialog />)
  await act(async () => { openSpaceCreate() })
  return r
}

describe('SpaceCreateDialog — public location', () => {
  beforeEach(() => { post.mockReset(); cleanup() })

  it('module exports exist', () => {
    expect(typeof SpaceCreateDialog).toBe('function')
    expect(typeof openSpaceCreate).toBe('function')
  })

  it('selecting Public reveals a location field and gates Create until coords are set', async () => {
    const { container } = await open()
    await act(async () => { fireEvent.input(nameInput(container), { target: { value: 'Neighbourhood' } }) })
    expect(container.querySelector('.sh-space-create-location')).toBeNull()

    await act(async () => { fireEvent.click(pickVisibility(container, 'public')) })
    await waitFor(() => expect(container.querySelector('.sh-space-create-location')).not.toBeNull())
    // Name is set but no coords yet → Create is disabled.
    expect(createBtn(container).disabled).toBe(true)

    const nums = container.querySelectorAll<HTMLInputElement>('.sh-space-create-location input[type="number"]')
    await act(async () => {
      fireEvent.input(nums[0], { target: { value: '52.52' } })
      fireEvent.input(nums[1], { target: { value: '13.405' } })
    })
    await waitFor(() => expect(createBtn(container).disabled).toBe(false))
  })

  it('submits lat/lon only for a public space', async () => {
    post.mockResolvedValue({ id: 's1' })
    const { container } = await open()
    await act(async () => { fireEvent.input(nameInput(container), { target: { value: 'Neighbourhood' } }) })
    await act(async () => { fireEvent.click(pickVisibility(container, 'public')) })
    const nums = container.querySelectorAll<HTMLInputElement>('.sh-space-create-location input[type="number"]')
    await act(async () => {
      fireEvent.input(nums[0], { target: { value: '52.52' } })
      fireEvent.input(nums[1], { target: { value: '13.405' } })
    })
    await act(async () => { fireEvent.click(createBtn(container)) })
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    const [url, body] = post.mock.calls[0]
    expect(url).toBe('/api/spaces')
    expect(body).toMatchObject({ space_type: 'public', lat: 52.52, lon: 13.405 })
  })

  it('does not send coords for a non-public space', async () => {
    post.mockResolvedValue({ id: 's2' })
    const { container } = await open()
    await act(async () => { fireEvent.input(nameInput(container), { target: { value: 'Family' } }) })
    // default visibility is private
    await act(async () => { fireEvent.click(createBtn(container)) })
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    const body = post.mock.calls[0][1] as Record<string, unknown>
    expect('lat' in body).toBe(false)
    expect('lon' in body).toBe(false)
  })
})
