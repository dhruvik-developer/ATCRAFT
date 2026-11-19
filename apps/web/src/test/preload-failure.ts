/**
 * A failed download as the production build sees it, and the stale-bundle
 * recovery (lib/stale-bundle.ts) watching it.
 *
 * Vite wraps every `import()` in its preload helper (`__vitePreload`), which on
 * a failure dispatches a cancelable `vite:preloadError` on window, carrying the
 * error as `payload`, and rethrows that same error unless a listener cancelled
 * the event - in which case the import resolves to `undefined`. Vitest has no
 * such helper, so a failed loader in a test reaches no listener at all: a test
 * of what a failed download does in production wraps its loader in
 * `asBuiltImport`.
 */

import { vi } from 'vitest'

import type { StaleBundleRecoveryHost } from '@/lib/stale-bundle'

/** `load` with Vite's preload helper around it, as `npm run build` emits every dynamic import. */
export function asBuiltImport<Module>(load: () => Promise<Module>): () => Promise<Module> {
  return () =>
    load().catch((error: unknown) => {
      const event = new Event('vite:preloadError', { cancelable: true }) as VitePreloadErrorEvent
      event.payload = error as Error
      window.dispatchEvent(event)
      if (!event.defaultPrevented) throw error
      return undefined as Module
    })
}

/**
 * The recovery's host for a test: a reload spy, a settable clock, storage that
 * starts empty, and the browser's own one-task deferral, with every decision
 * kept so a test can wait until it has been made - "did not reload" means
 * nothing until then.
 */
export function recoveryHost() {
  let stored: string | null = null
  const clock = { now: 1_000_000 }
  const decisions: Promise<void>[] = []
  const reload = vi.fn()
  const host: StaleBundleRecoveryHost = {
    reload,
    now: () => clock.now,
    readReloadedAt: () => stored,
    writeReloadedAt: (value) => {
      stored = value
    },
    defer: (task) => {
      decisions.push(
        new Promise((resolve) => {
          setTimeout(() => {
            task()
            resolve()
          }, 0)
        }),
      )
    },
  }
  return {
    host,
    reload,
    clock,
    /** Wait for `count` decisions in all to have been made. */
    async decided(count: number): Promise<void> {
      await vi.waitFor(() => {
        if (decisions.length < count) throw new Error(`${decisions.length} of ${count} decisions`)
      })
      await Promise.all(decisions)
    },
  }
}
