// Global test setup: unmount the testing-library tree after every test so
// rendered DOM never bleeds between tests. Vitest 4 + @testing-library/preact
// no longer auto-register this, so tests that don't manually call cleanup()
// would otherwise accumulate DOM and hit "multiple elements found".
import { cleanup } from '@testing-library/preact'
import { afterEach } from 'vitest'

afterEach(() => {
  cleanup()
})
