/**
 * The theme control: three states, one click to each, and audible as a choice.
 *
 * The interesting assertions are not "clicking dark makes it dark" but the two
 * that keep it honest: that `System` is reachable *after* pinning a theme (the
 * old control had no way back), and that the group reads as one choice of three
 * to a screen reader rather than as three unrelated buttons.
 */

import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ThemeProvider } from '@/app/theme-context'
import { ThemeToggle } from '@/components/theme-toggle'
import { STORAGE_KEYS } from '@/lib/constants'

function stubMatchMedia(prefersDark: boolean) {
  const query = {
    matches: prefersDark,
    media: '(prefers-color-scheme: dark)',
    addEventListener: () => {},
    removeEventListener: () => {},
  }
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => query),
  )
}

function renderToggle(props: { showLabels?: boolean } = {}) {
  return render(
    <ThemeProvider>
      <ThemeToggle {...props} />
    </ThemeProvider>,
  )
}

const group = () => screen.getByRole('radiogroup', { name: 'Theme' })
const option = (name: string) => within(group()).getByRole('radio', { name })
const isDark = () => document.documentElement.classList.contains('dark')

beforeEach(() => {
  localStorage.clear()
  document.documentElement.classList.remove('dark')
  stubMatchMedia(false)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('what it offers', () => {
  it('presents exactly the three states as one choice', () => {
    renderToggle()

    const options = within(group()).getAllByRole('radio')

    expect(options.map((o) => o.getAttribute('aria-label'))).toEqual(['Light', 'Dark', 'System'])
  })

  it('marks the current state, and only that one', () => {
    renderToggle()

    // Nothing stored, so System is current - not Light, which merely happens to
    // be what the system currently resolves to.
    expect(option('System')).toHaveAttribute('aria-checked', 'true')
    expect(option('Light')).toHaveAttribute('aria-checked', 'false')
    expect(option('Dark')).toHaveAttribute('aria-checked', 'false')
  })

  it('names each option even without visible labels, because the header is tight', () => {
    renderToggle()

    // The icons carry no text, so the accessible name is the only name.
    expect(option('Dark')).toBeVisible()
    expect(within(group()).queryByText('Dark')).toBeNull()
  })

  it('shows the words where there is room for them', () => {
    renderToggle({ showLabels: true })

    expect(within(group()).getByText('System')).toBeVisible()
  })
})

describe('choosing', () => {
  it('applies and persists an explicit theme', async () => {
    const user = userEvent.setup()
    renderToggle()

    await user.click(option('Dark'))

    expect(option('Dark')).toHaveAttribute('aria-checked', 'true')
    expect(isDark()).toBe(true)
    expect(localStorage.getItem(STORAGE_KEYS.theme)).toBe('dark')
  })

  it('gets back to following the system after a theme was pinned', async () => {
    // The gap this control was built to close: the previous one was a two-state
    // flip, so once an operator touched it their OS preference was ignored for
    // ever and nothing on screen offered a way back.
    const user = userEvent.setup()
    renderToggle()

    await user.click(option('Dark'))
    expect(localStorage.getItem(STORAGE_KEYS.theme)).toBe('dark')

    await user.click(option('System'))

    expect(option('System')).toHaveAttribute('aria-checked', 'true')
    expect(localStorage.getItem(STORAGE_KEYS.theme)).toBeNull()
    expect(isDark()).toBe(false)
  })

  it('reaches any state in one click, from any state', async () => {
    const user = userEvent.setup()
    renderToggle()

    await user.click(option('Dark'))
    expect(isDark()).toBe(true)

    await user.click(option('Light'))
    expect(isDark()).toBe(false)

    await user.click(option('Dark'))
    expect(isDark()).toBe(true)
  })
})
