/**
 * Light, dark, and following the operating system.
 *
 * One module owns the whole decision, because it is made in two places that
 * must not disagree: `main.tsx` paints the theme before React mounts (so a dark
 * operator never gets a white flash while `/auth/me` is in flight), and the
 * React provider owns it from then on. When those two read storage differently
 * the screen changes colour a moment after it loads, which looks like a bug in
 * the application rather than in the boot sequence.
 *
 * Three preferences, not two
 * --------------------------
 * The previous implementation stored only 'light' or 'dark', and wrote to
 * storage from an effect that ran on *mount*. So opening the ERP once pinned
 * whatever it had resolved, and the operating-system preference was never
 * consulted again - there was no way back to "follow my system", and a laptop
 * switching to dark at sunset did nothing. 'system' is therefore a real, stored
 * state, and it is the default.
 *
 * Storage is written only when a person chooses. Choosing 'system' *removes*
 * the key rather than writing the string, so an absent key and an explicit
 * "follow the system" are the same state - there is no way to end up in a
 * fourth, undefined one.
 */

import { STORAGE_KEYS } from '@/lib/constants'

/** What the user asked for. `system` defers to the OS, and keeps deferring. */
export type ThemePreference = 'light' | 'dark' | 'system'

/** What is actually painted. `system` resolves to one of these. */
export type ResolvedTheme = 'light' | 'dark'

export const THEME_PREFERENCES: readonly ThemePreference[] = ['light', 'dark', 'system'] as const

/** The default: defer to the operating system until told otherwise. */
export const DEFAULT_THEME_PREFERENCE: ThemePreference = 'system'

/** The class Tailwind's `darkMode: ['class']` looks for on `<html>`. */
export const DARK_CLASS = 'dark'

const MEDIA_QUERY = '(prefers-color-scheme: dark)'

function isThemePreference(value: unknown): boolean {
  return value === 'light' || value === 'dark' || value === 'system'
}

/**
 * The OS preference, or `light` where it cannot be asked.
 *
 * `matchMedia` is absent in some embedded webviews and in a bare jsdom, and
 * calling it there throws during module import - early enough that nothing has
 * rendered to show an error.
 */
export function systemTheme(): ResolvedTheme {
  try {
    return window.matchMedia?.(MEDIA_QUERY).matches ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

/**
 * The stored preference, or the default.
 *
 * Reading `localStorage` throws outright in a private window with site data
 * blocked, so this is guarded; an unreadable store is the same as an unset one.
 * A value written by an older build - or by hand - that is not one of the three
 * is discarded rather than trusted.
 */
export function readThemePreference(): ThemePreference {
  try {
    const stored = localStorage.getItem(STORAGE_KEYS.theme)
    return isThemePreference(stored) ? (stored as ThemePreference) : DEFAULT_THEME_PREFERENCE
  } catch {
    return DEFAULT_THEME_PREFERENCE
  }
}

/**
 * Persist an explicit choice. `system` clears the key - see the module note.
 *
 * Guarded for the same reason as the read: a browser that refuses to store the
 * preference must still apply it for this session, not fail the interaction.
 */
export function writeThemePreference(preference: ThemePreference): void {
  try {
    if (preference === 'system') localStorage.removeItem(STORAGE_KEYS.theme)
    else localStorage.setItem(STORAGE_KEYS.theme, preference)
  } catch {
    // Storage unavailable. The theme still applies for this session.
  }
}

/** What `preference` means right now. */
export function resolveTheme(preference: ThemePreference): ResolvedTheme {
  return preference === 'system' ? systemTheme() : preference
}

/**
 * Paint it.
 *
 * `color-scheme` is set alongside the class so the pieces the page does not
 * draw itself follow too - form controls, scrollbars, and the canvas behind an
 * overscroll. Without it a dark ERP scrolls to reveal a white gutter.
 */
export function applyTheme(theme: ResolvedTheme): void {
  const root = document.documentElement
  root.classList.toggle(DARK_CLASS, theme === 'dark')
  root.style.colorScheme = theme
}

/**
 * Watch the OS preference. Returns an unsubscribe function.
 *
 * Only meaningful while the preference is `system`; the provider subscribes and
 * unsubscribes accordingly rather than listening always and filtering, so a
 * pinned theme cannot be moved by the OS through a missed condition.
 *
 * `addEventListener` on a MediaQueryList is the modern spelling and the only
 * one used here; the older `addListener` is not worth carrying for the browsers
 * this ERP supports. A missing `matchMedia` yields a no-op unsubscribe rather
 * than an exception, so a webview without it degrades to a fixed theme.
 */
export function watchSystemTheme(onChange: (theme: ResolvedTheme) => void): () => void {
  let query: MediaQueryList | undefined
  try {
    query = window.matchMedia?.(MEDIA_QUERY)
  } catch {
    return () => {}
  }
  if (!query?.addEventListener) return () => {}

  const handler = (event: MediaQueryListEvent) => onChange(event.matches ? 'dark' : 'light')
  query.addEventListener('change', handler)
  return () => query.removeEventListener('change', handler)
}

/**
 * Read storage and paint, before React exists.
 *
 * `main.tsx` calls this at module scope. It is deliberately the same three
 * functions the provider uses, so the first frame and every frame after it are
 * decided by one piece of logic.
 */
export function applyStoredTheme(): ResolvedTheme {
  const theme = resolveTheme(readThemePreference())
  applyTheme(theme)
  return theme
}
