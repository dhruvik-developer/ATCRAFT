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

const BASE = process.env.SWEEP_BASE ?? 'http://localhost:4173'
const EMAIL = process.env.E2E_ADMIN_EMAIL ?? 'admin@atcraft.example'
const PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? 'ChangeMe!Atcraft2026'

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
  '/production', '/production/work-orders', '/assembly', '/job-work',
  '/quality', '/quality/templates', '/packing', '/dispatch',
  '/finance', '/finance/vendor-invoices', '/finance/payments', '/finance/accounts',
  '/documents', '/reports', '/audit',
  '/admin/users', '/admin/organisation',
  '/notifications', '/profile',
]

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()

// ---------------------------------------------------------------- log in ----
await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' })
// getByLabel(/password/i) is ambiguous: the field's own "Show password" toggle
// carries a matching aria-label. Target the textbox role explicitly.
await page.getByRole('textbox', { name: /email/i }).fill(EMAIL)
await page.getByRole('textbox', { name: /password/i }).fill(PASSWORD)
await page.getByRole('button', { name: /sign in|log ?in/i }).click()
await page.waitForURL((u) => !u.pathname.includes('/login'), { timeout: 20000 })
console.log(`logged in, landed on ${new URL(page.url()).pathname}`)

// ------------------------------------------------------------- the sweep ----
const results = []

for (const route of STATIC_ROUTES) {
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
    await page.goto(`${BASE}${route}`, { waitUntil: 'domcontentloaded', timeout: 25000 })
    // Let lazy chunks resolve and the first data round-trip settle.
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {})
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
