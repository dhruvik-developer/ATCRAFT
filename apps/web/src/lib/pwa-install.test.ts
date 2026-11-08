import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  _resetInstallPromptForTests,
  _setDeferredInstallPromptForTests,
  ensureInstallPromptListener,
  getDeferredInstallPrompt,
  isIosDevice,
  isStandaloneDisplay,
  promptPwaInstall,
  type BeforeInstallPromptEvent,
} from './pwa-install'

afterEach(() => {
  _resetInstallPromptForTests()
  vi.unstubAllGlobals()
})

describe('pwa-install', () => {
  it('reports standalone when display-mode matches', () => {
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: query.includes('standalone'),
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }))
    expect(isStandaloneDisplay()).toBe(true)
  })

  it('detects iOS from the user agent', () => {
    vi.stubGlobal('navigator', {
      userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)',
      platform: 'iPhone',
      maxTouchPoints: 5,
    })
    expect(isIosDevice()).toBe(true)
  })

  it('captures beforeinstallprompt and runs the deferred prompt', async () => {
    const listeners = new Map<string, EventListener>()
    vi.stubGlobal('window', {
      ...window,
      addEventListener: (type: string, listener: EventListener) => {
        listeners.set(type, listener)
      },
      dispatchEvent: (event: Event) => {
        listeners.get(event.type)?.(event)
        return true
      },
    })
    ensureInstallPromptListener()
    const prompt = vi.fn(async () => undefined)
    const userChoice = Promise.resolve({ outcome: 'accepted' as const, platform: 'web' })
    const event = {
      preventDefault: vi.fn(),
      prompt,
      userChoice,
    } as unknown as BeforeInstallPromptEvent
    listeners.get('beforeinstallprompt')?.(event as unknown as Event)
    expect(getDeferredInstallPrompt()).toBe(event)
    await expect(promptPwaInstall()).resolves.toBe('accepted')
    expect(prompt).toHaveBeenCalledOnce()
    expect(getDeferredInstallPrompt()).toBeNull()
  })

  it('returns unavailable when no deferred prompt exists', async () => {
    _setDeferredInstallPromptForTests(null)
    await expect(promptPwaInstall()).resolves.toBe('unavailable')
  })
})
