/**
 * The theme provider, and the two things its predecessor got wrong.
 *
 * The old `useTheme` lived inside `AppLayout`, held only 'light' | 'dark', and
 * wrote to `localStorage` from an effect keyed on the theme - which runs on
 * mount. So simply opening the ERP pinned whatever it had resolved: the
 * operating-system preference was consulted exactly once, ever, and there was
 * no state that meant "keep following it". A laptop switching to dark at sunset
 * did nothing, and no sequence of clicks could restore the link.
 *
 * Both of those are asserted here, because both are invisible in a screenshot.
 */

import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ThemeProvider, useTheme } from '@/app/theme-context'
import { STORAGE_KEYS } from '@/lib/constants'

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
    listenerCount: () => listeners.size,
    emit: (matches: boolean) => {
      query.matches = matches
      listeners.forEach((handler) => handler({ matches } as MediaQueryListEvent))
    },
  }
}

/** Shows what the provider decided, and offers a way to change it. */
function Probe() {
  const { preference, theme, setPreference } = useTheme()
  return (
    <div>
      <span data-testid="preference">{preference}</span>
      <span data-testid="theme">{theme}</span>
      <button type="button" onClick={() => setPreference('light')}>
        pick light
      </button>
      <button type="button" onClick={() => setPreference('dark')}>
        pick dark
      </button>
      <button type="button" onClick={() => setPreference('system')}>
        pick system
      </button>
    </div>
  )
}

const isDark = () => document.documentElement.classList.contains('dark')

beforeEach(() => {
  localStorage.clear()
  document.documentElement.classList.remove('dark')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('what it starts as', () => {
  it('follows the system when nothing was chosen', () => {
    stubMatchMedia(true)

    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    )

    expect(screen.getByTestId('preference')).toHaveTextContent('system')
    expect(screen.getByTestId('theme')).toHaveTextContent('dark')
    expect(isDark()).toBe(true)
  })

  it('honours a stored choice over the system', () => {
    stubMatchMedia(true)
    localStorage.setItem(STORAGE_KEYS.theme, 'light')

    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    )

    expect(screen.getByTestId('theme')).toHaveTextContent('light')
    expect(isDark()).toBe(false)
  })

  it('does not write to storage merely because it mounted', async () => {
    // The defect this whole module exists to fix. Persisting from a mount
    // effect silently opted every operator out of following their OS, on their
    // first visit, without them choosing anything.
    stubMatchMedia(true)

    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    )

    expect(localStorage.getItem(STORAGE_KEYS.theme)).toBeNull()
  })
})

describe('choosing', () => {
  it('pins the theme and persists it', async () => {
    const user = userEvent.setup()
    stubMatchMedia(true)

    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    )
    await user.click(screen.getByRole('button', { name: 'pick light' }))

    expect(screen.getByTestId('theme')).toHaveTextContent('light')
    expect(isDark()).toBe(false)
    expect(localStorage.getItem(STORAGE_KEYS.theme)).toBe('light')
  })

  it('hands the decision back when "system" is chosen again', async () => {
    // The other half of the defect: once pinned, there was no route back.
    const user = userEvent.setup()
    const media = stubMatchMedia(true)

    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    )
    await user.click(screen.getByRole('button', { name: 'pick light' }))
    expect(isDark()).toBe(false)

    await user.click(screen.getByRole('button', { name: 'pick system' }))

    expect(screen.getByTestId('preference')).toHaveTextContent('system')
    expect(screen.getByTestId('theme')).toHaveTextContent('dark')
    expect(isDark()).toBe(true)
    expect(localStorage.getItem(STORAGE_KEYS.theme)).toBeNull()
    expect(media.listenerCount()).toBe(1)
  })
})

describe('following the system while asked to', () => {
  it('repaints when the OS preference changes', async () => {
    const media = stubMatchMedia(false)

    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    )
    expect(isDark()).toBe(false)

    await vi.waitFor(() => expect(media.listenerCount()).toBe(1))
    // The OS fires this, not React - so it has to be wrapped, or the state
    // update it causes lands outside act() and React says so.
    act(() => media.emit(true))

    await vi.waitFor(() => expect(screen.getByTestId('theme')).toHaveTextContent('dark'))
    expect(isDark()).toBe(true)
  })

  it('ignores the OS once a theme is pinned', async () => {
    const user = userEvent.setup()
    const media = stubMatchMedia(false)

    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    )
    await user.click(screen.getByRole('button', { name: 'pick light' }))

    expect(media.listenerCount()).toBe(0)
    act(() => media.emit(true))

    expect(screen.getByTestId('theme')).toHaveTextContent('light')
    expect(isDark()).toBe(false)
  })
})

describe('using it wrongly', () => {
  it('throws outside a provider rather than rendering a dead control', () => {
    // A default here would render a toggle that changes nothing - the kind of
    // defect that ships because it looks like it works.
    const quiet = vi.spyOn(console, 'error').mockImplementation(() => {})
    const suppressErrorEvent = (event: ErrorEvent) => event.preventDefault()
    window.addEventListener('error', suppressErrorEvent)

    try {
      expect(() => render(<Probe />)).toThrow(/ThemeProvider/)
    } finally {
      window.removeEventListener('error', suppressErrorEvent)
      quiet.mockRestore()
    }
  })
})
