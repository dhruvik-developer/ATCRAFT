/**
 * The theme decision, which is made in two places that must agree.
 *
 * `main.tsx` paints before React mounts and `ThemeProvider` owns it afterwards;
 * both call the functions here, so these tests pin the contract between them.
 *
 * The behaviour worth pinning is not "a toggle flips a class" - it is the two
 * things the previous implementation got wrong. It stored only 'light' or
 * 'dark' and wrote from an effect that ran on mount, so opening the ERP once
 * pinned the theme for ever: there was no way back to following the operating
 * system, and a laptop switching to dark at sunset did nothing.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { STORAGE_KEYS } from '@/lib/constants'
import {
  DEFAULT_THEME_PREFERENCE,
  applyStoredTheme,
  applyTheme,
  readThemePreference,
  resolveTheme,
  systemTheme,
  watchSystemTheme,
  writeThemePreference,
} from '@/lib/theme'

/** Stand in for the OS preference, with a handle to fire a change at listeners. */
function stubMatchMedia(prefersDark: boolean) {
  const listeners = new Set<(event: MediaQueryListEvent) => void>()
  const query = {
    matches: prefersDark,
    media: '(prefers-color-scheme: dark)',
    addEventListener: (_: string, handler: (event: MediaQueryListEvent) => void) =>
      listeners.add(handler),
    removeEventListener: (_: string, handler: (event: MediaQueryListEvent) => void) =>
      listeners.delete(handler),
  }
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => query),
  )
  return {
    query,
    listenerCount: () => listeners.size,
    emit: (matches: boolean) => {
      query.matches = matches
      listeners.forEach((handler) => handler({ matches } as MediaQueryListEvent))
    },
  }
}

beforeEach(() => {
  localStorage.clear()
  document.documentElement.classList.remove('dark')
  document.documentElement.style.colorScheme = ''
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('what is stored', () => {
  it('defaults to following the system when nothing is stored', () => {
    expect(readThemePreference()).toBe('system')
    expect(DEFAULT_THEME_PREFERENCE).toBe('system')
  })

  it('round-trips an explicit choice', () => {
    writeThemePreference('dark')
    expect(readThemePreference()).toBe('dark')

    writeThemePreference('light')
    expect(readThemePreference()).toBe('light')
  })

  it('stores "system" by removing the key, so absent and explicit are one state', () => {
    // Otherwise there are two spellings of the same intent, and the pre-React
    // paint and the provider can disagree about which one means what.
    writeThemePreference('dark')
    writeThemePreference('system')

    expect(localStorage.getItem(STORAGE_KEYS.theme)).toBeNull()
    expect(readThemePreference()).toBe('system')
  })

  it('discards a value it did not write', () => {
    localStorage.setItem(STORAGE_KEYS.theme, 'solarized')

    expect(readThemePreference()).toBe('system')
  })

  it('survives storage that throws, rather than failing the boot', () => {
    // A private window with site data blocked throws on access, and this runs
    // before anything has rendered that could show an error.
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('denied')
    })
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('denied')
    })

    expect(readThemePreference()).toBe('system')
    expect(() => writeThemePreference('dark')).not.toThrow()

    getItem.mockRestore()
    setItem.mockRestore()
  })
})

describe('what it resolves to', () => {
  it('takes an explicit preference literally, whatever the system says', () => {
    stubMatchMedia(true)

    expect(resolveTheme('light')).toBe('light')
    expect(resolveTheme('dark')).toBe('dark')
  })

  it('asks the system only for "system"', () => {
    stubMatchMedia(true)
    expect(resolveTheme('system')).toBe('dark')

    stubMatchMedia(false)
    expect(resolveTheme('system')).toBe('light')
  })

  it('falls back to light where matchMedia does not exist', () => {
    // Some embedded webviews have no matchMedia at all, and this is called at
    // module scope - an exception here means a blank page.
    vi.stubGlobal('matchMedia', undefined)

    expect(systemTheme()).toBe('light')
    expect(resolveTheme('system')).toBe('light')
  })
})

describe('what it paints', () => {
  it('sets the class Tailwind reads, and unsets it again', () => {
    applyTheme('dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)

    applyTheme('light')
    expect(document.documentElement.classList.contains('dark')).toBe(false)
  })

  it('sets color-scheme, so scrollbars and controls follow', () => {
    // Without this a dark ERP scrolls past its content into a white gutter,
    // and native form controls stay light on a dark form.
    applyTheme('dark')
    expect(document.documentElement.style.colorScheme).toBe('dark')

    applyTheme('light')
    expect(document.documentElement.style.colorScheme).toBe('light')
  })

  it('paints from storage before React exists', () => {
    stubMatchMedia(false)
    writeThemePreference('dark')

    expect(applyStoredTheme()).toBe('dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)
  })

  it('paints the system theme when nothing was chosen', () => {
    stubMatchMedia(true)

    expect(applyStoredTheme()).toBe('dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)
  })
})

describe('following the system', () => {
  it('reports a change to the listener', () => {
    const media = stubMatchMedia(false)
    const seen: string[] = []

    watchSystemTheme((theme) => seen.push(theme))
    media.emit(true)
    media.emit(false)

    expect(seen).toEqual(['dark', 'light'])
  })

  it('unsubscribes, so a pinned theme stops listening', () => {
    const media = stubMatchMedia(false)

    const stop = watchSystemTheme(() => {})
    expect(media.listenerCount()).toBe(1)

    stop()
    expect(media.listenerCount()).toBe(0)
  })

  it('returns a usable unsubscribe where matchMedia is missing', () => {
    vi.stubGlobal('matchMedia', undefined)

    const stop = watchSystemTheme(() => {})

    expect(() => stop()).not.toThrow()
  })
})
