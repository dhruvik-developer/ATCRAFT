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
import { readyAdminToken } from '../lib/session.mjs'

/** Screens worth checking: a list, a dashboard, a form-heavy one, a chart one.

These are application routes (`route-loaders.ts`), not API paths. The inventory
list lives at `/inventory`; `/inventory/balances` is the API that feeds it.
 */
const SCREENS = [
  ['/', 'dashboard'],
  ['/inventory', 'inventory list'],
  ['/items', 'item master'],
  ['/finance', 'finance'],
  ['/notifications', 'notifications'],
]

/** Relative luminance, so "is this actually dark" is a number and not an opinion. */
function luminance(rgb) {
  if (
    rgb.length !== 3 ||
    rgb.some((channel) => !Number.isFinite(channel) || channel < 0 || channel > 255)
  ) {
    throw new Error(`Invalid sRGB triplet: ${JSON.stringify(rgb)}`)
  }
  const [r, g, b] = rgb.map((channel) => {
    const encoded = channel / 255
    return encoded <= 0.04045 ? encoded / 12.92 : ((encoded + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

// Theme-direction thresholds were calibrated on encoded sRGB brightness.
// Keep that heuristic separate from WCAG's linear-light contrast calculation:
// a saturated primary button can be dark without being a wrong-theme panel.
function brightness([r, g, b]) {
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
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
// A fresh seed forces a password change before any screen will open. Do that
// through the API first, or the form login below parks on /change-password and
// the theme checks time out on a page that has no shell.
await readyAdminToken()
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
      // Canvas resolves every browser-supported CSS colour space into sRGB.
      // Parsing digits misreads oklab(), and treating translucent backgrounds
      // as opaque measures a colour the user never sees.
      const canvas = document.createElement('canvas')
      canvas.width = canvas.height = 1
      const ctx = canvas.getContext('2d', { colorSpace: 'srgb', willReadFrequently: true })
      if (!ctx) throw new Error('Cannot measure theme colours without a 2D canvas.')
      const paint = (color) => {
        if (!CSS.supports('color', color)) throw new Error(`Unknown CSS colour: ${color}`)
        // An invalid canvas colour assignment silently retains the old value.
        // Two sentinels distinguish that from a supported CSS colour.
        ctx.fillStyle = '#010203'
        ctx.fillStyle = color
        const first = ctx.fillStyle
        ctx.fillStyle = '#040506'
        ctx.fillStyle = color
        if (ctx.fillStyle !== first) throw new Error(`Canvas cannot resolve CSS colour: ${color}`)
        ctx.fillRect(0, 0, 1, 1)
      }
      const pixel = () => [...ctx.getImageData(0, 0, 1, 1).data]
      const normalize = (color) => {
        ctx.clearRect(0, 0, 1, 1)
        paint(color)
        return pixel()
      }
      const rootStyle = getComputedStyle(document.documentElement)
      const semanticFills = ['--primary', '--success', '--warning', '--destructive'].map(
        (token) => {
          const channels = rootStyle.getPropertyValue(token).trim()
          if (!channels) throw new Error(`Missing theme colour token: ${token}`)
          const rgba = normalize(`hsl(${channels})`)
          if (rgba[3] !== 255) throw new Error(`Semantic theme colour must be opaque: ${token}`)
          return rgba
        },
      )
      const background = (el) => {
        const ancestors = []
        for (let current = el; current; current = current.parentElement) ancestors.unshift(current)
        ctx.clearRect(0, 0, 1, 1)
        for (const ancestor of ancestors) paint(getComputedStyle(ancestor).backgroundColor)
        const rgba = pixel()
        if (rgba[3] !== 255)
          throw new Error('Theme background has no opaque ancestor to composite against.')
        return rgba.slice(0, 3)
      }
      const read = (el) => {
        const s = getComputedStyle(el)
        const bg = background(el)
        paint(s.color)
        return { bg, fg: pixel().slice(0, 3), css: s.backgroundColor }
      }
      const body = read(document.body)
      // Every panel that paints its own surface - a card that hardcoded white
      // would show up here and nowhere else.
      const surfaces = [...document.querySelectorAll('main *')]
        .filter((el) => {
          return normalize(getComputedStyle(el).backgroundColor)[3] > 0
        })
        .slice(0, 40)
        .map((el) => {
          const css = getComputedStyle(el).backgroundColor
          const rgba = normalize(css)
          return {
            css,
            rgb: background(el),
            // Semantic fills deliberately carry status/action colour in both
            // themes. Only exact matches to this theme's four opaque tokens
            // are exempt; arbitrary colours and wrong neutral panels still fail.
            semantic: semanticFills.some((fill) => fill.every((channel, i) => channel === rgba[i])),
          }
        })
      return {
        dark: document.documentElement.classList.contains('dark'),
        colorScheme: document.documentElement.style.colorScheme,
        body,
        surfaces: [
          ...new Map(surfaces.map((surface) => [JSON.stringify(surface), surface])).values(),
        ],
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

    const bodyBrightness = brightness(seen.body.bg)
    const wantDark = theme === 'dark'
    if (wantDark && bodyBrightness > 0.35) {
      failures.push(
        `${label}: background ${seen.body.css} is not dark (brightness ${bodyBrightness.toFixed(2)})`,
      )
    }
    if (!wantDark && bodyBrightness < 0.65) {
      failures.push(
        `${label}: background ${seen.body.css} is not light (brightness ${bodyBrightness.toFixed(2)})`,
      )
    }

    // Text must stay readable against the page, in both themes.
    const ratio = contrast(seen.body.bg, seen.body.fg)
    if (ratio < 4.5) {
      failures.push(`${label}: body text contrast ${ratio.toFixed(1)}:1 is below 4.5:1`)
    }

    // A surface that stayed light while the page went dark is a hardcoded colour.
    const wrong = seen.surfaces.filter((surface) => {
      if (surface.semantic) return false
      const value = brightness(surface.rgb)
      return wantDark ? value > 0.6 : value < 0.25
    })
    if (wrong.length) {
      failures.push(
        `${label}: ${wrong.length} surface(s) did not follow the theme, e.g. ${wrong[0].css} rendered as rgb(${wrong[0].rgb.join(', ')})`,
      )
    }

    console.log(
      `${label.padEnd(30)} bg ${`rgb(${seen.body.bg.join(', ')})`.padEnd(20)} contrast ${ratio.toFixed(1)}:1  ` +
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
