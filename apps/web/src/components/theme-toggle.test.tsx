/**
 * The theme control: three states, one click to each, and audible as a choice.
 *
 * The interesting assertions are not "clicking dark makes it dark" but the two
 * that keep it honest: that `System` is reachable *after* pinning a theme (the
 * old control had no way back), and that the group reads as one choice of three
 * to a screen reader rather than as three unrelated buttons - and behaves as one
 * from the keyboard: a single Tab stop, with the arrow keys to choose.
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
    expect(option('System')).toBeChecked()
    expect(option('Light')).not.toBeChecked()
    expect(option('Dark')).not.toBeChecked()
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

    expect(option('Dark')).toBeChecked()
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

    expect(option('System')).toBeChecked()
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

describe('from the keyboard', () => {
  // A radio group is one Tab stop that lands on the current option, and the
  // arrow keys choose within it. ARIA roles on buttons announce that and do
  // none of it, which is what this control shipped as.
  it('is one Tab stop, landing on the current option', async () => {
    const user = userEvent.setup()
    render(
      <ThemeProvider>
        <button type="button">Before</button>
        <ThemeToggle />
        <button type="button">After</button>
      </ThemeProvider>,
    )

    await user.tab()
    expect(screen.getByRole('button', { name: 'Before' })).toHaveFocus()
    await user.tab()
    expect(option('System')).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: 'After' })).toHaveFocus()
  })

  it('changes and persists the theme with the arrow keys', async () => {
    const user = userEvent.setup()
    renderToggle()

    await user.tab()
    expect(option('System')).toHaveFocus()

    await user.keyboard('{ArrowLeft}')
    expect(option('Dark')).toHaveFocus()
    expect(option('Dark')).toBeChecked()
    expect(isDark()).toBe(true)
    expect(localStorage.getItem(STORAGE_KEYS.theme)).toBe('dark')

    await user.keyboard('{ArrowLeft}')
    expect(option('Light')).toBeChecked()
    expect(localStorage.getItem(STORAGE_KEYS.theme)).toBe('light')

    await user.keyboard('{ArrowRight}')
    expect(option('Dark')).toBeChecked()
    expect(isDark()).toBe(true)
  })

  it('keeps two toggles on one page as separate groups', async () => {
    // The sign-in page and a header can both hold one; a shared group name
    // would make the arrows walk from one into the other.
    render(
      <ThemeProvider>
        <ThemeToggle />
        <ThemeToggle />
      </ThemeProvider>,
    )

    const [first, second] = screen.getAllByRole('radiogroup', { name: 'Theme' })
    const nameOf = (scope: HTMLElement) =>
      within(scope).getByRole('radio', { name: 'Light' }).getAttribute('name')
    expect(nameOf(first!)).toBeTruthy()
    expect(nameOf(first!)).not.toBe(nameOf(second!))
  })
})
