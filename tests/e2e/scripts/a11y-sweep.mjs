/**
 * Accessibility sweep: axe-core against every screen, signed in, with real data.
 *
 * Run against the *populated* database. An empty table has no rows, so it has no
 * row actions, no status chips and no icon-only buttons - which is exactly where
 * accessibility defects live. A sweep over empty screens reports a clean bill of
 * health it has not earned.
 *
 * Only serious and critical violations fail the sweep. Moderate and minor ones
 * are counted and listed so they can be worked through deliberately, rather than
 * drowning the signal on the first run - the point of this file is to be run
 * routinely, and a check nobody can ever get to zero is a check people learn to
 * ignore.
 *
 * WCAG 2.1 AA is the target, plus the "best-practice" rules axe ships, because
 * several of them (button-name, link-name, aria-allowed-attr) catch the exact
 * failure that makes a screen unusable with a keyboard or a screen reader while
 * looking perfect on a monitor.
 *
 * A11Y_ROUTES=/items,/warehouses A11Y_COLOR_SCHEME=dark node scripts/a11y-sweep.mjs
 * The JSON report records each screen's failures as well as aggregated rules;
 * a failed API request or a redirected session cannot count as a clean scan.
 */

import AxeBuilder from '@axe-core/playwright'
import { chromium } from '@playwright/test'
import { writeFileSync } from 'node:fs'

import { BASE_URL as BASE } from '../lib/env.mjs'
import { createNavigator, errorToastFailures } from '../lib/navigation.mjs'
import { readyAdminToken, signInThroughForm } from '../lib/session.mjs'

const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'best-practice']

const ROUTES = [
  '/',
  '/items',
  '/warehouses',
  '/warehouses/locations',
  '/masters',
  '/inventory',
  '/inventory/ledger',
  '/inventory/reservations',
  '/inventory/movements',
  '/inventory/tracking',
  '/bom',
  '/routing',
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
  '/production/planning',
  '/production/shift-roster',
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
  '/finance/credit-notes',
  '/sales-returns',
  '/documents',
  '/reports',
  '/audit',
  '/admin/users',
  '/admin/organisation',
  '/admin/settings',
  '/notifications',
  '/profile',
]
const routeFilters = process.env.A11Y_ROUTES?.split(',').filter(Boolean) ?? []
const routes = routeFilters.length ? ROUTES.filter((route) => routeFilters.includes(route)) : ROUTES
if (!routes.length) throw new Error('No screens match A11Y_ROUTES.')
const colorScheme = process.env.A11Y_COLOR_SCHEME ?? 'light'
if (!['light', 'dark'].includes(colorScheme)) throw new Error('Invalid A11Y_COLOR_SCHEME.')

const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  colorScheme,
})
const page = await context.newPage()
const { navigate, settle } = createNavigator(page)
const results = []

try {
  // The login screen is itself a screen, and the first one anybody meets.
  await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('heading', { name: 'Sign in', exact: true }).waitFor()
  const loginScan = await new AxeBuilder({ page }).withTags(TAGS).analyze()
  results.push({
    route: '/login',
    violations: loginScan.violations,
    failures: await errorToastFailures(page),
  })

  // The seeded administrator is forced through a password change before it can
  // reach any screen; `readyAdminToken` completes that through the API on a
  // fresh database, and is one successful login otherwise.
  await readyAdminToken()
  await signInThroughForm(page)
  await settle()

  for (const route of routes) {
    const failures = []
    const onPageError = (error) => failures.push(`render: ${error.message}`)
    const onResponse = (response) => {
      const path = new URL(response.url()).pathname
      if (path.startsWith('/api/') && response.status() >= 400)
        failures.push(`HTTP ${response.status()} ${response.request().method()} ${path}`)
    }
    const onRequestFailure = (request) => {
      const reason = request.failure()?.errorText ?? 'network request failed'
      if (reason.includes('ERR_ABORTED')) return
      const path = new URL(request.url()).pathname
      if (path.startsWith('/api/') || ['script', 'stylesheet'].includes(request.resourceType()))
        failures.push(`network: ${request.method()} ${path}: ${reason}`)
    }
    page.on('pageerror', onPageError)
    page.on('response', onResponse)
    page.on('requestfailed', onRequestFailure)
    let violations = []
    try {
      // Keep the in-memory session and wait for the actual route, not a clean
      // sign-in/error screen reached while a refresh or a request is failing.
      await navigate(route)
      if (
        await page
          .getByRole('heading', {
            name: /something went wrong|you do not have access/i,
          })
          .count()
      )
        failures.push('render-boundary')
      const scan = await new AxeBuilder({ page }).withTags(TAGS).analyze()
      violations = scan.violations
      failures.push(...(await errorToastFailures(page)))
    } catch (error) {
      failures.push(String(error))
    } finally {
      page.off('pageerror', onPageError)
      page.off('response', onResponse)
      page.off('requestfailed', onRequestFailure)
    }
    results.push({ route, violations, failures: [...new Set(failures)] })

    const bySeverity = (level) => violations.filter((v) => v.impact === level).length
    const serious = bySeverity('critical') + bySeverity('serious')
    const other = violations.length - serious
    console.log(
      `${(serious || failures.length ? 'FAIL' : 'ok').padEnd(5)} ${route.padEnd(32)} ` +
        `serious+critical=${String(serious).padStart(2)} moderate+minor=${String(other).padStart(2)}` +
        (failures.length ? ` ${[...new Set(failures)].join('; ')}` : ''),
    )
    for (const violation of violations.filter((rule) =>
      ['serious', 'critical'].includes(rule.impact),
    )) {
      console.log(`      ${violation.id}: ${violation.nodes[0]?.html?.slice(0, 300)}`)
    }
  }
} finally {
  await browser.close()
}

// --- aggregate by rule, because one component repeats across many screens ----
const byRule = new Map()
for (const { route, violations } of results) {
  for (const v of violations) {
    const entry = byRule.get(v.id) ?? {
      id: v.id,
      impact: v.impact,
      help: v.help,
      helpUrl: v.helpUrl,
      routes: new Set(),
      nodes: 0,
      sample: v.nodes[0]?.html?.slice(0, 200),
    }
    entry.routes.add(route)
    entry.nodes += v.nodes.length
    byRule.set(v.id, entry)
  }
}

const rules = [...byRule.values()]
  .map((r) => ({ ...r, routes: [...r.routes] }))
  .sort((a, b) => {
    const order = { critical: 0, serious: 1, moderate: 2, minor: 3 }
    return (order[a.impact] ?? 4) - (order[b.impact] ?? 4) || b.nodes - a.nodes
  })

writeFileSync(
  process.env.SWEEP_OUT ?? 'a11y-sweep.json',
  JSON.stringify({ colorScheme, screens: results, rules }, null, 2),
  'utf-8',
)

console.log('\n--- distinct rules violated, worst first ---')
for (const r of rules) {
  console.log(
    `  [${(r.impact ?? 'n/a').padEnd(8)}] ${r.id.padEnd(28)} ${r.nodes} node(s) on ${r.routes.length} screen(s)`,
  )
  console.log(`      ${r.help}`)
  if (r.sample) console.log(`      e.g. ${r.sample.replace(/\s+/g, ' ')}`)
}

const blocking = rules.filter((r) => r.impact === 'critical' || r.impact === 'serious')
const failedScreens = results.filter((result) => result.failures.length)
console.log(
  `\n${results.length} screens | ${rules.length} distinct rules | ` +
    `${blocking.length} serious or critical | ${failedScreens.length} screens failed to render`,
)
if (blocking.length || failedScreens.length) process.exitCode = 1
