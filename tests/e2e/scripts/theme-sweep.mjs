/**
 * Prove the theme actually applies, on real screens, in a real browser.
 *
 * The unit tests assert the decision logic; this asserts the consequence. It
 * walks the main screens in each theme and reads the *computed* background and
 * text colour of the page and of the panels on it, because "the class is on
 * <html>" and "this screen is legible in the dark" are different claims - a
 * screen that hardcoded a colour would pass the first and fail the second.
 *
 * Run against a live stack:
 *   E2E_BASE_URL=http://127.0.0.1 node scripts/theme-sweep.mjs
 */

import { chromium } from '@playwright/test'

import { ADMIN_EMAIL, ADMIN_PASSWORD, BASE_URL } from '../lib/env.mjs'

/** Screens worth checking: a list, a dashboard, a form-heavy one, a chart one. */
const SCREENS = [
  ['/', 'dashboard'],
  ['/inventory/balances', 'inventory list'],
  ['/products/items', 'item master'],
  ['/finance/receivables', 'finance + charts'],
  ['/notifications', 'notifications'],
]

/** Relative luminance, so "is this actually dark" is a number and not an opinion. */
function luminance(rgb) {
  const [r, g, b] = rgb.match(/\d+/g).slice(0, 3).map(Number)
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

const failures = []

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()

// --- the login page, before any session exists -------------------------------
await page.goto(`${BASE_URL}/login`)
await page.waitForLoadState('domcontentloaded')
await page.getByRole('button', { name: /sign in/i }).waitFor({ timeout: 30_000 })

const themeGroup = page.getByRole('radiogroup', { name: 'Theme' })
if ((await themeGroup.count()) === 0) {
  failures.push('login page: no theme control - the one screen seen before signing in')
} else {
  for (const option of ['Light', 'Dark', 'System']) {
    if ((await themeGroup.getByRole('radio', { name: option }).count()) === 0) {
      failures.push(`login page: theme control is missing the ${option} option`)
    }
  }
  await themeGroup.getByRole('radio', { name: 'Dark' }).click()
  const dark = await page.evaluate(() => document.documentElement.classList.contains('dark'))
  if (!dark) failures.push('login page: choosing Dark did not apply the dark class')
  await themeGroup.getByRole('radio', { name: 'Light' }).click()
  const light = await page.evaluate(() => !document.documentElement.classList.contains('dark'))
  if (!light) failures.push('login page: choosing Light did not remove the dark class')
  console.log('login page: three-state control present and applying')
}

// --- sign in -----------------------------------------------------------------
await page.getByLabel('Email').fill(ADMIN_EMAIL)
await page.locator('input[type="password"]').first().fill(ADMIN_PASSWORD)
await page.getByRole('button', { name: /sign in/i }).click()
await page.waitForURL((url) => !url.pathname.includes('/login'), { timeout: 30_000 })
// NOT networkidle: the shell polls unread notifications on an interval, so the
// network is never idle in this application and that wait can only time out.
await page.getByRole('radiogroup', { name: 'Theme' }).waitFor({ timeout: 30_000 })

// --- every screen, in both themes -------------------------------------------
for (const theme of ['light', 'dark']) {
  await page.evaluate((t) => {
    localStorage.setItem('atcraft.theme', t)
  }, theme)

  for (const [path, name] of SCREENS) {
    await page.goto(`${BASE_URL}${path}`)
    await page.waitForLoadState('domcontentloaded')
    await page.locator('main').waitFor({ timeout: 30_000 })
    // Let the lazy chunk mount and the first query paint.
    await page.waitForTimeout(1200)

    const seen = await page.evaluate(() => {
      const read = (el) => {
        const s = getComputedStyle(el)
        return { bg: s.backgroundColor, fg: s.color }
      }
      const body = read(document.body)
      // Every panel that paints its own surface - a card that hardcoded white
      // would show up here and nowhere else.
      const surfaces = [...document.querySelectorAll('main *')]
        .filter((el) => {
          const bg = getComputedStyle(el).backgroundColor
          return bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent'
        })
        .slice(0, 40)
        .map((el) => getComputedStyle(el).backgroundColor)
      return {
        dark: document.documentElement.classList.contains('dark'),
        colorScheme: document.documentElement.style.colorScheme,
        body,
        surfaces: [...new Set(surfaces)],
      }
    })

    const label = `${name} [${theme}]`
    if (seen.dark !== (theme === 'dark')) {
      failures.push(`${label}: dark class is ${seen.dark}, expected ${theme === 'dark'}`)
      continue
    }
    if (seen.colorScheme !== theme) {
      failures.push(`${label}: color-scheme is "${seen.colorScheme}", expected "${theme}"`)
    }

    const bodyLum = luminance(seen.body.bg)
    const wantDark = theme === 'dark'
    if (wantDark && bodyLum > 0.35) {
      failures.push(`${label}: background ${seen.body.bg} is not dark (luminance ${bodyLum.toFixed(2)})`)
    }
    if (!wantDark && bodyLum < 0.65) {
      failures.push(`${label}: background ${seen.body.bg} is not light (luminance ${bodyLum.toFixed(2)})`)
    }

    // Text must stay readable against the page, in both themes.
    const ratio = contrast(seen.body.bg, seen.body.fg)
    if (ratio < 4.5) {
      failures.push(`${label}: body text contrast ${ratio.toFixed(1)}:1 is below 4.5:1`)
    }

    // A surface that stayed light while the page went dark is a hardcoded colour.
    const wrong = seen.surfaces.filter((bg) => {
      const l = luminance(bg)
      return wantDark ? l > 0.6 : l < 0.25
    })
    if (wrong.length) {
      failures.push(`${label}: ${wrong.length} surface(s) did not follow the theme, e.g. ${wrong[0]}`)
    }

    console.log(
      `${label.padEnd(30)} bg ${seen.body.bg.padEnd(20)} contrast ${ratio.toFixed(1)}:1  ` +
        `${seen.surfaces.length} surfaces checked`,
    )
  }
}

await browser.close()

if (failures.length) {
  console.log(`\n${failures.length} PROBLEM(S):`)
  for (const f of failures) console.log('  -', f)
  process.exit(1)
}
console.log('\nevery screen follows the theme, in both themes')
