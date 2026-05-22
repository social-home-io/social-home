/* eslint-disable @typescript-eslint/no-explicit-any */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render } from '@testing-library/preact'
import { instanceConfig } from '@/store/instance'

// The component captures the boot bundle hash at module-load by
// scraping ``<script type="module" src="…/assets/index-{hash}.js">``
// off ``document``. We stage the tag BEFORE importing so the
// module-level capture picks it up.
document.head.insertAdjacentHTML(
  'beforeend',
  '<script type="module" src="./assets/index-BOOT123.js"></script>',
)
const { SpaUpdateBanner, _test } = await import('./SpaUpdateBanner')

beforeEach(() => {
  instanceConfig.value = null
  _test.dismissedFor.value = null
})

describe('SpaUpdateBanner', () => {
  it('captures the boot bundle hash from the loaded script tag', () => {
    expect(_test.bootBundleHash()).toBe('BOOT123')
  })

  it('renders nothing when the backend reports the same hash', () => {
    instanceConfig.value = {
      mode: 'standalone',
      instance_name: 'X', instance_id: null,
      capabilities: [],
      setup_required: false,
      spa_bundle_hash: 'BOOT123',
    }
    const { container } = render(<SpaUpdateBanner />)
    expect(container.querySelector('.sh-update-banner')).toBeNull()
  })

  it('renders nothing when the backend reports a null hash (dev mode)', () => {
    instanceConfig.value = {
      mode: 'standalone',
      instance_name: 'X', instance_id: null,
      capabilities: [],
      setup_required: false,
      spa_bundle_hash: null,
    }
    const { container } = render(<SpaUpdateBanner />)
    expect(container.querySelector('.sh-update-banner')).toBeNull()
  })

  it('renders the banner when the backend hash differs', () => {
    instanceConfig.value = {
      mode: 'standalone',
      instance_name: 'X', instance_id: null,
      capabilities: [],
      setup_required: false,
      spa_bundle_hash: 'NEWER456',
    }
    const { container } = render(<SpaUpdateBanner />)
    const banner = container.querySelector('.sh-update-banner')
    expect(banner).not.toBeNull()
    expect(banner?.textContent ?? '').toMatch(/newer version/i)
    expect(banner?.querySelector('.sh-update-banner__btn--primary')?.textContent)
      .toBe('Reload')
  })

  it('hides the banner after the user clicks "Later" for this hash', () => {
    instanceConfig.value = {
      mode: 'standalone',
      instance_name: 'X', instance_id: null,
      capabilities: [],
      setup_required: false,
      spa_bundle_hash: 'NEWER456',
    }
    const { container, rerender } = render(<SpaUpdateBanner />)
    const ghost = container.querySelector(
      '.sh-update-banner__btn--ghost',
    ) as HTMLButtonElement
    ghost.click()
    rerender(<SpaUpdateBanner />)
    expect(container.querySelector('.sh-update-banner')).toBeNull()
    expect(_test.dismissedFor.value).toBe('NEWER456')
  })

  it('resurfaces the banner when a NEW hash lands after dismissal', () => {
    _test.dismissedFor.value = 'NEWER456'
    instanceConfig.value = {
      mode: 'standalone',
      instance_name: 'X', instance_id: null,
      capabilities: [],
      setup_required: false,
      spa_bundle_hash: 'EVEN_NEWER789',  // a second deploy
    }
    const { container } = render(<SpaUpdateBanner />)
    expect(container.querySelector('.sh-update-banner')).not.toBeNull()
  })

  it('clicking Reload calls window.location.reload', () => {
    instanceConfig.value = {
      mode: 'standalone',
      instance_name: 'X', instance_id: null,
      capabilities: [],
      setup_required: false,
      spa_bundle_hash: 'NEWER456',
    }
    const reload = vi.fn()
    // jsdom's ``location`` is non-writable; redefine via descriptor.
    Object.defineProperty(window, 'location', {
      value: { reload, hash: '', href: 'http://localhost/' } as any,
      configurable: true,
    })
    const { container } = render(<SpaUpdateBanner />)
    const primary = container.querySelector(
      '.sh-update-banner__btn--primary',
    ) as HTMLButtonElement
    primary.click()
    expect(reload).toHaveBeenCalledTimes(1)
  })
})
