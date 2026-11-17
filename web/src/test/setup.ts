import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

afterEach(() => {
  cleanup()
  sessionStorage.clear()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// What Radix (Select, Checkbox, Switch) needs from a browser that jsdom lacks.
globalThis.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} }
Element.prototype.hasPointerCapture ??= () => false
Element.prototype.releasePointerCapture ??= () => {}
Element.prototype.scrollIntoView ??= () => {}
