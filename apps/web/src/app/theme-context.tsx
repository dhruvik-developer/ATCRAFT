/**
 * The theme, for every screen rather than only the ones inside `AppLayout`.
 *
 * The toggle used to live in `AppLayout`, which the login page never renders -
 * so the one screen an operator sees before signing in was the one screen whose
 * theme they could not change. The provider is mounted at the root instead, and
 * `ThemeToggle` can therefore sit anywhere.
 *
 * The decision logic itself is in `@/lib/theme`, shared with the pre-React paint
 * in `main.tsx`; this file is only the React binding around it.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

import {
  applyTheme,
  readThemePreference,
  resolveTheme,
  watchSystemTheme,
  writeThemePreference,
} from '@/lib/theme'
import type { ResolvedTheme, ThemePreference } from '@/lib/theme'

interface ThemeContextValue {
  /** What the user asked for - `system` included. */
  preference: ThemePreference
  /** What is painted right now. */
  theme: ResolvedTheme
  /** Choose explicitly. `system` hands the decision back to the OS. */
  setPreference: (preference: ThemePreference) => void
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

export function ThemeProvider({ children }: { children: ReactNode }) {
  // Read once, lazily. `main.tsx` has already painted from this same value, so
  // the first render agrees with what is on screen and nothing flashes.
  const [preference, setPreferenceState] = useState<ThemePreference>(readThemePreference)
  const [theme, setTheme] = useState<ResolvedTheme>(() => resolveTheme(readThemePreference()))

  // Paint whenever the resolved theme changes. Note what this effect does NOT
  // do: write to storage. The previous implementation persisted from an effect
  // like this one, so merely opening the ERP pinned the theme and the operating
  // system was never consulted again. Storage is written in `setPreference`,
  // where a person actually chose something.
  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  // Follow the OS, but only while asked to. Subscribing conditionally rather
  // than always-and-filtering means a pinned theme cannot be moved by the OS
  // through a condition someone forgets to check.
  useEffect(() => {
    if (preference !== 'system') return undefined
    setTheme(resolveTheme('system'))
    return watchSystemTheme(setTheme)
  }, [preference])

  const setPreference = useCallback((next: ThemePreference) => {
    writeThemePreference(next)
    setPreferenceState(next)
    setTheme(resolveTheme(next))
  }, [])

  const value = useMemo(
    () => ({ preference, theme, setPreference }),
    [preference, theme, setPreference],
  )

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

/**
 * Throws outside a provider rather than returning a plausible default.
 *
 * A silent fallback here would render a toggle that changes nothing, which is
 * the kind of defect that reaches a user because it looks like it works.
 */
export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext)
  if (!value) throw new Error('useTheme must be used within a ThemeProvider')
  return value
}
