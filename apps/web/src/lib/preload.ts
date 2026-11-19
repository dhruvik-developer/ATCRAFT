/**
 * Eager loading's two primitives: whether the browser has asked to save data,
 * and how to run a download as soon as the main thread has a moment.
 *
 * The screens and forms themselves are preloaded by `preloadRoutes`
 * (app/route-loaders.ts) and `lazyDialog` (components/lazy-dialog.ts); both
 * start straight after a paint and only yield to work the person is waiting on.
 */

import { PRELOAD_IDLE_TIMEOUT_MS } from '@/lib/constants'

type NetworkInformationLike = { saveData?: boolean }

/** The person's own browser setting to save data - the one opt-out from eager loading. */
export function browserSavesData(): boolean {
  if (typeof navigator === 'undefined') return false
  const connection = (navigator as Navigator & { connection?: NetworkInformationLike }).connection
  return connection?.saveData === true
}

/**
 * Run `task` when the browser is next idle, and at most PRELOAD_IDLE_TIMEOUT_MS
 * later even if it never is. Returns a cancel function.
 */
export function whenIdle(task: () => void): () => void {
  if (typeof window.requestIdleCallback === 'function') {
    const handle = window.requestIdleCallback(task, { timeout: PRELOAD_IDLE_TIMEOUT_MS })
    return () => window.cancelIdleCallback(handle)
  }
  // Safari has no requestIdleCallback: a zero-delay timer still lets the paint
  // and any pending input go first.
  const handle = window.setTimeout(task, 0)
  return () => window.clearTimeout(handle)
}
