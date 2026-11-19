/**
 * The route loaders back both `React.lazy` and the sidebar prefetch, so every
 * sidebar destination must have one - a nav entry without a loader would
 * silently lose its hover warm-up and, worse, could not be routed at all.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createElement } from 'react'

import { navigation } from './navigation'
import {
  __resetPrefetch,
  prefetchRoute,
  preloadRoutes,
  reachableScreens,
  routeLoaders,
} from './route-loaders'

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
    const spy = vi
      .spyOn(routeLoaders, '/audit')
      .mockResolvedValue({ default: () => createElement('div') })
    prefetchRoute('/audit')
    prefetchRoute('/audit')
    prefetchRoute('/audit')
    expect(spy).toHaveBeenCalledTimes(1)
    // Exercise the loader contract without cold-compiling an unrelated page.
    // The production build and browser suite validate the real page modules.
    const loaded = await spy.mock.results[0]?.value
    expect(typeof loaded.default).toBe('function')
    spy.mockRestore()
  })

  it('forgets a failed warm-up so the click can retry it', async () => {
    const spy = vi
      .spyOn(routeLoaders, '/reports')
      .mockResolvedValue({ default: () => createElement('div') })
      .mockRejectedValueOnce(new Error('offline'))
    prefetchRoute('/reports')
    await Promise.resolve()
    await Promise.resolve()
    prefetchRoute('/reports')
    expect(spy).toHaveBeenCalledTimes(2)
    spy.mockRestore()
  })
})

describe('reachableScreens', () => {
  it('adds the detail pages opened from a list the person can reach', () => {
    const screens = reachableScreens(['/items', '/mrp'])

    expect(screens).toEqual(
      expect.arrayContaining(['/items', '/mrp', '/items/:itemId', '/mrp/runs/:runId']),
    )
    expect(screens).not.toContain('/bom/:bomId')
    expect(screens).not.toContain('/vendor/orders/:orderId')
  })
})

describe('preloadRoutes (eager loading)', () => {
  function stubLoaders(paths: (keyof typeof routeLoaders)[]) {
    return paths.map((path) =>
      vi.spyOn(routeLoaders, path).mockResolvedValue({ default: () => createElement('div') }),
    )
  }

  it('downloads every listed screen once, a few at a time', async () => {
    const spies = stubLoaders(['/audit', '/reports', '/documents'])

    preloadRoutes(['/audit', '/reports', '/documents', '/audit', '/nowhere'], {
      enabled: true,
      concurrency: 2,
    })

    await vi.waitFor(() => {
      for (const spy of spies) expect(spy).toHaveBeenCalledTimes(1)
    })
    for (const spy of spies) spy.mockRestore()
  })

  it('stops downloading once cancelled', async () => {
    const spies = stubLoaders(['/audit', '/reports'])

    const cancel = preloadRoutes(['/audit', '/reports'], { enabled: true, concurrency: 1 })
    cancel()
    await new Promise((resolve) => setTimeout(resolve, 20))

    for (const spy of spies) expect(spy).not.toHaveBeenCalled()
    for (const spy of spies) spy.mockRestore()
  })

  it('downloads nothing when eager loading is off or the browser asks to save data', async () => {
    const spies = stubLoaders(['/audit'])
    preloadRoutes(['/audit'], { enabled: false })

    Object.defineProperty(navigator, 'connection', {
      value: { saveData: true },
      configurable: true,
    })
    preloadRoutes(['/audit'], { enabled: true })
    await new Promise((resolve) => setTimeout(resolve, 20))

    expect(spies[0]).not.toHaveBeenCalled()
    Reflect.deleteProperty(navigator, 'connection')
    for (const spy of spies) spy.mockRestore()
  })
})
