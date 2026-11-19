/**
 * Recover from a stale bundle after a deploy.
 *
 * `index.html` is served `no-store` while the hashed chunks it names are
 * immutable and removed on redeploy, so a tab left open across a release holds
 * an index whose next `import('/assets/<page>-<oldhash>.js')` 404s. Vite raises
 * `vite:preloadError` for exactly that, and the import rejects: the boundary
 * catches it and reports a crash - and "Try again" can never fix it, because
 * React's `lazy` caches the rejected promise. Reloading picks up the new index
 * and the new hashes.
 *
 * Only a download a person is waiting on may reload the page. Eager loading
 * fetches every screen and form in the background, and a failure there - a
 * deploy mid-burst, a flaky plant Wi-Fi, an edge limit refusing one chunk - used
 * to reload the page under someone typing into a form that had nothing to do
 * with it. A background download records its failure (`noteBackgroundFailure`
 * in lib/preload.ts) from its own `.catch`, which runs after the event, so the
 * decision waits one task for it; a recorded failure is left alone, and the
 * screen or form downloads again when it is opened.
 *
 * So the event is never cancelled: the import has to reject for the background
 * catch to see its error. Cancelling bought nothing anyway - a cancelled import
 * resolves to `undefined`, and `lazy` throws reading its `default`, so the
 * boundary showed for the moment before the reload landed either way.
 *
 * Guarded by *when* the last such reload happened, not by whether one has: a
 * stale bundle is cured by the very next load, so a second failure moments
 * later is a chunk that genuinely is not being served - a broken deploy - and
 * reloading again would spin. CHUNK_RELOAD_LOOP_WINDOW_MS is long enough to
 * cover the reload and short enough that a later deploy, in the same long-lived
 * tab, still recovers by itself. Clearing the flag on a successful boot would
 * not work: the entry loads fine in both cases, and it is a lazy route chunk
 * that 404s.
 */

import { CHUNK_RELOAD_LOOP_WINDOW_MS, STORAGE_KEYS } from '@/lib/constants'
import { isBackgroundFailure } from '@/lib/preload'
import { readStorage, writeStorage } from '@/lib/storage'

/** What the recovery touches outside itself; each defaults to the browser's own. */
export interface StaleBundleRecoveryHost {
  reload: () => void
  /** Milliseconds since the epoch. */
  now: () => number
  /** When the last stale-chunk reload ran, as stored; null when never, or unreadable. */
  readReloadedAt: () => string | null
  writeReloadedAt: (value: string) => void
  /** Run `task` once the current task, and every promise reaction it queued, has finished. */
  defer: (task: () => void) => void
}

const browserHost: StaleBundleRecoveryHost = {
  reload: () => window.location.reload(),
  now: () => Date.now(),
  readReloadedAt: () => readStorage(STORAGE_KEYS.chunkReloadedAt, 'session'),
  writeReloadedAt: (value) => {
    writeStorage(STORAGE_KEYS.chunkReloadedAt, value, 'session')
  },
  defer: (task) => {
    window.setTimeout(task, 0)
  },
}

/** Listen for `vite:preloadError` on `target`; returns the function that stops listening. */
export function installStaleBundleRecovery(
  target: Window = window,
  host: Partial<StaleBundleRecoveryHost> = {},
): () => void {
  const { reload, now, readReloadedAt, writeReloadedAt, defer } = { ...browserHost, ...host }

  const onPreloadError = (event: VitePreloadErrorEvent) => {
    const error: unknown = event.payload
    defer(() => {
      if (isBackgroundFailure(error)) return

      const at = now()
      // With no session storage (private window, blocked site data) this reads
      // as never reloaded, and the reload goes ahead: a stale bundle is by far
      // the likelier cause of a preload failure.
      const last = Number(readReloadedAt()) || 0

      // Reloaded for this a moment ago and it failed again - the chunk is really
      // gone. Leave it to the error boundary rather than spinning.
      if (at - last < CHUNK_RELOAD_LOOP_WINDOW_MS) return

      // Without storage the loop guard cannot arm; the reload below is still the
      // right move for the common case.
      writeReloadedAt(String(at))
      reload()
    })
  }

  target.addEventListener('vite:preloadError', onPreloadError)
  return () => target.removeEventListener('vite:preloadError', onPreloadError)
}
