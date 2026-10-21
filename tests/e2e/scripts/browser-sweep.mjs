/**
 * Browser sweep: open every route in a real Chromium against the real API and
 * report what a user would actually see.
 *
 * This exists because everything else in this project checks a proxy for the
 * screen rather than the screen: tsc checks types, eslint checks style, the
 * contract check compares two files, and the permission guards read source.
 * All of them passed on a page whose `can` was unbound, and on five screens
 * gating actions on rights their endpoints do not enforce.
 *
 * What is recorded per route:
 *   - uncaught page errors and console errors (a render crash shows here first)
 *   - any API response >= 400 the page itself triggered
 *   - whether the error boundary or a "denied" state took over
 *   - how much text actually rendered, to catch a route that resolves to an
 *     empty shell without throwing
 */

import { chromium } from '@playwright/test'
import { writeFileSync } from 'node:fs'

import { BASE_URL as BASE } from '../lib/env.mjs'
import { readyAdminToken, signInThroughForm } from '../lib/session.mjs'

// Every static route. Parameterised routes are resolved from live data below.
const STATIC_ROUTES = [
  '/', '/items', '/warehouses', '/warehouses/locations',
  '/inventory', '/inventory/ledger', '/inventory/reservations', '/inventory/movements',
  '/bom', '/routing', '/masters',
  '/sales-orders', '/customers', '/dealers',
  '/mrp',
  '/procurement/requisitions', '/procurement/purchase-orders', '/procurement/goods-receipts',
  '/procurement/rfqs', '/procurement/quotations', '/procurement/purchase-returns',
  '/vendors', '/transporters',
  '/production', '/production/work-orders', '/production/planning', '/production/shift-roster', '/assembly', '/job-work',
  '/quality', '/quality/templates', '/packing', '/dispatch',
  '/finance', '/finance/sales-invoices', '/finance/vendor-invoices', '/finance/payments', '/finance/accounts',
  '/finance/credit-notes', '/sales-returns',
  '/documents', '/reports', '/audit',
  '/admin/users', '/admin/organisation', '/admin/settings',
  '/notifications', '/profile',
]

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()

// ---------------------------------------------------------------- log in ----
// The seeded administrator is forced through a password change before it can
// reach any screen; `readyAdminToken` completes that through the API on a
// fresh database, and is one successful login otherwise.
await readyAdminToken()
await signInThroughForm(page)
console.log(`logged in, landed on ${new URL(page.url()).pathname}`)

// ------------------------------------------------------------ navigation ----
// Routes are opened client-side, as a person clicking through the app opens
// them: one full page load for the sign-in, then routing. A `page.goto` per
// route reloads the SPA, which drops the in-memory access token, so every
// route began with two 401s on GET /auth/me before the refresh cookie restored
// the session. That marked all of them 'HTTP' and buried any real failure in
// the one column meant to show it - and it rotated the refresh cookie once per
// route. A route the push cannot render still gets a full load as a fallback.

const SETTLE_TYPES = new Set(['fetch', 'xhr', 'script', 'stylesheet'])
let inflight = 0
const counted = (r) => SETTLE_TYPES.has(r.resourceType())
page.on('request', (r) => {
  if (counted(r)) inflight += 1
})
const finished = (r) => {
  if (counted(r)) inflight = Math.max(0, inflight - 1)
}
page.on('requestfinished', finished)
page.on('requestfailed', finished)

/**
 * Wait until no data or code request has been in flight for half a second.
 * `waitForLoadState('networkidle')` cannot do this after a push: the document
 * reached that state long ago, so it resolves at once.
 */
async function settle(limitMs = 15_000) {
  const started = Date.now()
  let quietSince = null
  while (Date.now() - started < limitMs) {
    if (inflight === 0) {
      quietSince ??= Date.now()
      if (Date.now() - quietSince >= 500) return
    } else {
      quietSince = null
    }
    await page.waitForTimeout(100)
  }
}

async function push(target) {
  await page.evaluate((p) => {
    window.history.pushState({}, '', p)
    window.dispatchEvent(new PopStateEvent('popstate'))
  }, target)
}

/** A push to the route already showing renders nothing new, so step off it first. */
async function leave(route) {
  if (new URL(page.url()).pathname !== route) return
  await push(route === '/' ? '/profile' : '/')
  await settle()
}

async function navigate(route) {
  await push(route)
  try {
    await page.locator('main').waitFor({ timeout: 25_000 })
  } catch {
    await page.goto(`${BASE}${route}`, { waitUntil: 'domcontentloaded', timeout: 25_000 })
  }
  await settle()
}

// ------------------------------------------------------------- the sweep ----
const results = []

for (const route of STATIC_ROUTES) {
  await leave(route)
  const errors = []
  const consoleErrors = []
  const httpFailures = []

  const onPageError = (e) => errors.push(String(e?.message ?? e).slice(0, 300))
  const onConsole = (m) => {
    if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 300))
  }
  const onResponse = (r) => {
    const u = new URL(r.url())
    if (u.pathname.startsWith('/api/') && r.status() >= 400) {
      httpFailures.push(`${r.status()} ${r.request().method()} ${u.pathname}${u.search}`)
    }
  }

  page.on('pageerror', onPageError)
  page.on('console', onConsole)
  page.on('response', onResponse)

  let nav = 'ok'
  try {
    // Lets lazy chunks resolve and the first data round-trip settle.
    await navigate(route)
  } catch (e) {
    nav = `NAV FAILED: ${String(e).slice(0, 200)}`
  }

  const body = await page.evaluate(() => document.body.innerText).catch(() => '')
  const boundary = /something went wrong|unexpected error|reload the page/i.test(body)
  const denied = /you do not have|not authorised|not authorized|permission/i.test(body)

  page.off('pageerror', onPageError)
  page.off('console', onConsole)
  page.off('response', onResponse)

  results.push({
    route,
    nav,
    chars: body.length,
    boundary,
    denied,
    errors,
    consoleErrors,
    httpFailures,
  })

  const flag =
    nav !== 'ok' || boundary || errors.length
      ? 'BROKEN'
      : httpFailures.length
        ? 'HTTP'
        : consoleErrors.length
          ? 'console'
          : body.length < 400
            ? 'THIN'
            : 'ok'
  console.log(
    `${flag.padEnd(8)} ${route.padEnd(34)} chars=${String(body.length).padStart(5)} ` +
      `err=${errors.length} console=${consoleErrors.length} http4xx=${httpFailures.length}`,
  )
}

writeFileSync(
  process.env.SWEEP_OUT ?? 'browser-sweep.json',
  JSON.stringify(results, null, 2),
  'utf-8',
)

const broken = results.filter((r) => r.nav !== 'ok' || r.boundary || r.errors.length)
const http = results.filter((r) => r.httpFailures.length)
const thin = results.filter((r) => r.chars < 400 && !r.boundary)
console.log(
  `\n${results.length} routes | broken=${broken.length} http-failures=${http.length} thin=${thin.length}`,
)

await browser.close()
