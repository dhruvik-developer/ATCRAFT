/**
 * Responsive sweep: every screen, and the primary dialog on it, at phone,
 * tablet and desktop widths, in a real Chromium against the real API.
 *
 * "Each screen is responsive" is a claim about layout, and nothing else in the
 * project measures layout: tsc and eslint do not know what a pixel is, the unit
 * tests run in jsdom where every box is 0 x 0, and the browser sweep beside
 * this one opens every route at 1440px only. A `shrink-0` on a wrapping group
 * (see CLAUDE.md) passed all of them and pushed five buttons off a phone.
 *
 * What is measured per screen and viewport:
 *   - horizontal page overflow: `document.documentElement.scrollWidth` beyond
 *     the viewport. A page that scrolls sideways on a phone is the defect in
 *     its purest form, and the one people report first.
 *   - the elements that stick out: anything whose right edge is past the
 *     viewport and that is not inside a container that scrolls sideways on
 *     purpose (a data table is allowed to, a page header is not). The five
 *     widest are recorded with tag, class and text so the offender can be
 *     found from the report alone.
 *   - the primary dialog, when the screen has a "New ..." / "Add ..." /
 *     "Record ..." control: opened, and checked to fit the viewport with its
 *     footer reachable - either it fits outright or something in it scrolls.
 *
 * Run against a live stack with data:
 *   E2E_BASE_URL=http://127.0.0.1:5173 node scripts/responsive-sweep.mjs
 *
 * SWEEP_OUT names the JSON report (default responsive-sweep.json); SWEEP_SHOTS
 * a directory for a phone-width screenshot of every flagged screen.
 */

import { mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'

import { chromium } from '@playwright/test'

import { BASE_URL as BASE } from '../lib/env.mjs'
import { readyAdminToken, signInThroughForm } from '../lib/session.mjs'

const VIEWPORTS = [
  { name: 'phone', width: 375, height: 667 },
  { name: 'tablet', width: 768, height: 1024 },
  { name: 'desktop', width: 1440, height: 900 },
]

// Every static screen, including the tabbed ones. Detail screens are resolved
// from the first row of their list below, exactly as capture-screens does.
const STATIC_ROUTES = [
  '/',
  '/items',
  '/warehouses',
  '/warehouses/locations',
  '/inventory',
  '/inventory/ledger',
  '/inventory/reservations',
  '/inventory/movements',
  '/bom',
  '/routing',
  '/masters?tab=categories',
  '/masters?tab=units',
  '/masters?tab=conversions',
  '/masters?tab=attributes',
  '/masters?tab=brands',
  '/masters?tab=manufacturers',
  '/masters?tab=prices',
  '/sales-orders',
  '/customers',
  '/dealers',
  '/mrp',
  '/procurement/requisitions',
  '/procurement/purchase-orders',
  '/procurement/goods-receipts',
  '/procurement/rfqs',
  '/procurement/quotations',
  '/procurement/purchase-returns',
  '/vendors',
  '/transporters',
  '/production',
  '/production/work-orders',
  '/assembly',
  '/job-work',
  '/quality',
  '/quality/templates',
  '/packing',
  '/dispatch',
  '/finance',
  '/finance/sales-invoices',
  '/finance/vendor-invoices',
  '/finance/payments',
  '/finance/accounts',
  '/documents',
  '/reports',
  '/audit',
  '/admin/users',
  '/admin/organisation',
  '/admin/organisation?tab=cost-centres',
  '/notifications',
  '/profile',
]

const DETAIL_LISTS = {
  '/items': '/items',
  '/bom': '/bom',
  '/sales-orders': '/sales-orders',
  '/procurement/purchase-orders': '/procurement/purchase-orders',
  '/procurement/rfqs': '/procurement/rfqs',
  '/production': '/production',
  '/mrp': '/mrp/runs',
}

// Verbs that open a form. Not "run" or "post": those label actions that act at
// once - "Run MRP" started a real MRP run on every sweep, three per pass, and
// "Post receipt" or "Post adjustment" would move stock - and none of them
// opens a dialog, so the sweep only ever reported them as dialog errors.
const OPENER = /^(new|add|create|raise|record|invite|upload|register)\b/i
const OUT = process.env.SWEEP_OUT ?? 'responsive-sweep.json'
const SHOTS = process.env.SWEEP_SHOTS ?? null

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: VIEWPORTS[2] })
const page = await context.newPage()

/** Client-side navigation: one page load per run, then routing, so the refresh cookie rotates once. */
async function navigate(route) {
  const push = (target) =>
    page.evaluate((p) => {
      window.history.pushState({}, '', p)
      window.dispatchEvent(new PopStateEvent('popstate'))
    }, target)
  await push(route)
  try {
    await page.locator('main').waitFor({ timeout: 30_000 })
  } catch {
    await page.waitForTimeout(2000)
    await push(route)
    await page.locator('main').waitFor({ timeout: 30_000 })
  }
  await page.waitForTimeout(1500)
}

/**
 * Signed in, or parked on the sign-in screen? Decided from the route, not from
 * the sidebar: the shell's "Collapse" control is what the desktop sweeps look
 * for, and it does not exist at phone width, where the sidebar is a drawer.
 */
function isInApp() {
  const { pathname } = new URL(page.url())
  return !pathname.startsWith('/login') && !pathname.startsWith('/change-password')
}

async function ensureSignedIn() {
  if (isInApp()) return
  await signInThroughForm(page)
  await page.locator('main').waitFor({ timeout: 30_000 })
}

/** Measured inside the page: page overflow and the elements that cause it. */
function measure() {
  const vw = window.innerWidth
  const doc = document.documentElement
  const overflow = Math.max(0, doc.scrollWidth - vw)
  const scrollsSideways = (el) => {
    for (let n = el.parentElement; n; n = n.parentElement) {
      const o = getComputedStyle(n).overflowX
      if ((o === 'auto' || o === 'scroll') && n.scrollWidth > n.clientWidth) return true
    }
    return false
  }
  const offenders = []
  for (const el of document.querySelectorAll('body *')) {
    if (el.closest('[role="dialog"], [data-radix-popper-content-wrapper]')) continue
    const r = el.getBoundingClientRect()
    if (r.width === 0 || r.height === 0) continue
    if (r.right <= vw + 1) continue
    if (scrollsSideways(el)) continue
    offenders.push({
      over: Math.round(r.right - vw),
      tag: el.tagName.toLowerCase(),
      cls: (el.getAttribute('class') ?? '').slice(0, 120),
      text: (el.textContent ?? '').trim().replace(/\s+/g, ' ').slice(0, 60),
    })
  }
  offenders.sort((a, b) => b.over - a.over)
  // Keep the outermost offenders: a child inside a too-wide parent tells nothing new.
  return { overflow, offenders: offenders.slice(0, 5) }
}

/** The open dialog's fit: inside the viewport, footer reachable, scrolls when taller than the screen. */
function measureDialog() {
  const dialog = document.querySelector('[role="dialog"]')
  if (!dialog) return null
  const vw = window.innerWidth
  const vh = window.innerHeight
  const r = dialog.getBoundingClientRect()
  const scrollable = [dialog, ...dialog.querySelectorAll('*')].some((el) => {
    const o = getComputedStyle(el).overflowY
    return (o === 'auto' || o === 'scroll') && el.scrollHeight > el.clientHeight + 1
  })
  const buttons = [...dialog.querySelectorAll('button')].filter((b) => b.getBoundingClientRect().height > 0)
  const last = buttons.length ? buttons[buttons.length - 1].getBoundingClientRect() : null
  const overflowsDown = r.bottom > vh + 1
  return {
    title: (dialog.querySelector('h2, [id$="-title"]')?.textContent ?? '').trim().slice(0, 60),
    width: Math.round(r.width),
    height: Math.round(r.height),
    left: Math.round(r.left),
    right: Math.round(vw - r.right),
    sideways: r.left < -1 || r.right > vw + 1,
    tallerThanViewport: r.height > vh + 1 || overflowsDown,
    scrollable,
    footerReachable: !last || (last.bottom <= vh + 1 && last.top >= -1) || scrollable,
  }
}

async function firstRowHref(listRoute, prefix) {
  await navigate(listRoute)
  const links = page.locator(`main a[href^="${prefix}/"]`)
  // The list's first query paints whenever it paints; on a loaded machine that
  // is seconds, so wait for a link rather than for a fixed settle time.
  await links.first().waitFor({ timeout: 15_000 }).catch(() => {})
  const hrefs = await links.evaluateAll((els) => els.map((link) => link.getAttribute('href')))
  return hrefs.find(Boolean) ?? null
}

// ---------------------------------------------------------------- sign in ----
await readyAdminToken()
await signInThroughForm(page)
await page.locator('main').waitFor({ timeout: 30_000 })
console.log(`logged in, landed on ${new URL(page.url()).pathname}`)

// ----------------------------------------------------- resolve detail rows ----
const routes = [...STATIC_ROUTES]
for (const [listRoute, prefix] of Object.entries(DETAIL_LISTS)) {
  const href = await firstRowHref(listRoute, prefix)
  if (href) routes.push(href)
  else console.log(`  (no row on ${listRoute}: its detail screen is skipped)`)
}
if (SHOTS) mkdirSync(SHOTS, { recursive: true })

// ------------------------------------------------------------- the sweep ----
const results = []
let previous = null
for (const viewport of VIEWPORTS) {
  await page.setViewportSize({ width: viewport.width, height: viewport.height })
  console.log(`\n== ${viewport.name} ${viewport.width}x${viewport.height}`)
  for (const route of routes) {
    if (route === previous) await navigate('/')
    await navigate(route)
    previous = route
    await ensureSignedIn()
    if (new URL(page.url()).pathname + new URL(page.url()).search !== route) {
      await navigate(route)
    }

    const pageResult = await page.evaluate(measure).catch((e) => ({ overflow: -1, offenders: [], error: String(e) }))
    let dialog = null
    const opener = page.locator('main').getByRole('button', { name: OPENER }).first()
    if ((await opener.count()) > 0 && (await opener.isVisible().catch(() => false))) {
      const label = (await opener.textContent().catch(() => ''))?.trim() ?? ''
      try {
        await opener.click({ timeout: 5000 })
        await page.locator('[role="dialog"]').first().waitFor({ timeout: 8000 })
        await page.waitForTimeout(600)
        dialog = { opener: label, ...(await page.evaluate(measureDialog)) }
      } catch (e) {
        dialog = { opener: label, error: String(e).slice(0, 160) }
      }
      await page.keyboard.press('Escape').catch(() => {})
      await page
        .locator('[role="dialog"]')
        .first()
        .waitFor({ state: 'detached', timeout: 5000 })
        .catch(() => {})
    }

    const flags = []
    if (pageResult.overflow > 0) flags.push(`page-overflow:${pageResult.overflow}px`)
    if (pageResult.offenders.length) flags.push(`offenders:${pageResult.offenders.length}`)
    if (dialog && !dialog.error) {
      if (dialog.sideways) flags.push('dialog-sideways')
      if (dialog.tallerThanViewport && !dialog.scrollable) flags.push('dialog-clipped')
      if (!dialog.footerReachable) flags.push('dialog-footer-hidden')
    }
    if (dialog?.error) flags.push('dialog-error')

    results.push({ viewport: viewport.name, route, ...pageResult, dialog, flags })
    console.log(
      `${(flags.length ? 'FLAG' : 'ok').padEnd(5)} ${route.padEnd(36)} overflow=${String(pageResult.overflow).padStart(4)} ` +
        `${flags.join(' ')}${dialog ? ` [dialog: ${dialog.opener}]` : ''}`,
    )
    if (flags.length && SHOTS && viewport.name !== 'desktop') {
      const name = `${viewport.name}-${route.replace(/[^a-z0-9]+/gi, '_')}.png`
      await page.screenshot({ path: path.join(SHOTS, name), fullPage: false }).catch(() => {})
    }
  }
}

writeFileSync(OUT, JSON.stringify(results, null, 2), 'utf-8')
const flagged = results.filter((r) => r.flags.length)
console.log(`\n${results.length} screen/viewport pairs | flagged=${flagged.length}`)
for (const r of flagged) {
  console.log(`  ${r.viewport.padEnd(8)} ${r.route.padEnd(36)} ${r.flags.join(' ')}`)
  for (const o of r.offenders.slice(0, 2)) console.log(`      +${o.over}px <${o.tag} class="${o.cls}"> ${o.text}`)
}
await browser.close()
process.exit(flagged.some((r) => r.viewport !== 'desktop') ? 1 : 0)
