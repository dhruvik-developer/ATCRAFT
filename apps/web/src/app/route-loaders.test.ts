/**
 * The route loaders back both `React.lazy` and the sidebar prefetch, so every
 * sidebar destination must have one - a nav entry without a loader would
 * silently lose its hover warm-up and, worse, could not be routed at all.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { navigation } from './navigation'
import { __resetPrefetch, prefetchRoute, routeLoaders } from './route-loaders'

beforeEach(() => {
  __resetPrefetch()
})

describe('routeLoaders', () => {
  it('covers every sidebar destination', () => {
    const missing = navigation
      .flatMap((section) => section.items.map((item) => item.to))
      .filter((path) => !Object.hasOwn(routeLoaders, path))
    expect(missing).toEqual([])
  })

  it('keys every loader by an absolute path', () => {
    for (const path of Object.keys(routeLoaders)) {
      expect(path.startsWith('/')).toBe(true)
    }
  })
})

describe('prefetchRoute', () => {
  it('ignores a path with no loader', () => {
    expect(() => prefetchRoute('/nowhere')).not.toThrow()
    expect(() => prefetchRoute('https://example.com')).not.toThrow()
  })

  it('starts the import once and dedupes repeat calls', async () => {
    const spy = vi.spyOn(routeLoaders, '/audit')
    prefetchRoute('/audit')
    prefetchRoute('/audit')
    prefetchRoute('/audit')
    expect(spy).toHaveBeenCalledTimes(1)
    // Resolves to the page module, which is what `lazy` will pick up.
    const loaded = await spy.mock.results[0]?.value
    expect(typeof loaded.default).toBe('function')
    spy.mockRestore()
  })

  it('forgets a failed warm-up so the click can retry it', async () => {
    const spy = vi.spyOn(routeLoaders, '/reports').mockRejectedValueOnce(new Error('offline'))
    prefetchRoute('/reports')
    await Promise.resolve()
    await Promise.resolve()
    prefetchRoute('/reports')
    expect(spy).toHaveBeenCalledTimes(2)
    spy.mockRestore()
  })
})
