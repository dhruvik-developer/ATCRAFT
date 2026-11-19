/**
 * Eager loading's primitives: whether the browser has asked to save data, how
 * to run a download as soon as the main thread has a moment, and the record of
 * which failed downloads nobody was waiting on.
 *
 * The screens and forms themselves are preloaded by `preloadRoutes`
 * (app/route-loaders.ts), straight after the shell paints, and `lazyDialog`
 * (components/lazy-dialog.ts), as soon as the screen that holds the form has
 * loaded; both only yield to work the person is waiting on.
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

/**
 * The errors of failed downloads that nobody was waiting on.
 *
 * Every background download - the eager screens and forms, and the hover, focus
 * and sign-in warm-ups - ends in `.catch(noteBackgroundFailure)`. Vite rejects a
 * failed import with the very error it hands the stale-bundle recovery
 * (lib/stale-bundle.ts), which reads this record before it reloads: a reload for
 * a screen nobody has opened would throw away whatever the person is doing, a
 * half-filled form included. The screen or form downloads again when it is
 * opened, and a failure then is one the person is waiting on.
 *
 * Keyed by the error object itself, so a record cannot outlive the failure it
 * describes, and a fresh attempt - which fails with a new error - is never
 * mistaken for the background one.
 */
const backgroundFailures = new WeakSet<object>()

/** Record a background download's failure; the `.catch` handler of every eager or warm-up download. */
export function noteBackgroundFailure(error: unknown): void {
  if (typeof error === 'object' && error !== null) backgroundFailures.add(error)
}

/** Whether a background download recorded this error as its own failure. */
export function isBackgroundFailure(error: unknown): boolean {
  return typeof error === 'object' && error !== null && backgroundFailures.has(error)
}
