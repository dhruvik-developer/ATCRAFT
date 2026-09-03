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
 */

import AxeBuilder from '@axe-core/playwright'
import { chromium } from '@playwright/test'
import { writeFileSync } from 'node:fs'

const BASE = process.env.SWEEP_BASE ?? 'http://localhost:4173'
const EMAIL = process.env.E2E_ADMIN_EMAIL ?? 'admin@atcraft.example'
const PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? 'ChangeMe!Atcraft2026'

const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'best-practice']

const ROUTES = [
  '/', '/items', '/warehouses', '/warehouses/locations', '/masters',
  '/inventory', '/inventory/ledger', '/inventory/reservations', '/inventory/movements',
  '/bom', '/routing',
  '/sales-orders', '/customers', '/dealers',
  '/mrp',
  '/procurement/requisitions', '/procurement/purchase-orders', '/procurement/goods-receipts',
  '/procurement/rfqs', '/procurement/quotations', '/procurement/purchase-returns',
  '/vendors', '/transporters',
  '/production', '/production/work-orders', '/assembly', '/job-work',
  '/quality', '/quality/templates', '/packing', '/dispatch',
  '/finance', '/finance/vendor-invoices', '/finance/payments', '/finance/accounts',
  '/documents', '/reports', '/audit',
  '/admin/users', '/admin/organisation', '/notifications', '/profile',
]

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()

// The login screen is itself a screen, and the first one anybody meets.
await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' })
const loginScan = await new AxeBuilder({ page }).withTags(TAGS).analyze()

await page.getByRole('textbox', { name: /email/i }).fill(EMAIL)
await page.getByRole('textbox', { name: /password/i }).fill(PASSWORD)
await page.getByRole('button', { name: /sign in|log ?in/i }).click()
await page.waitForURL((u) => !u.pathname.includes('/login'), { timeout: 20000 })

const results = [{ route: '/login', violations: loginScan.violations }]

for (const route of ROUTES) {
  await page.goto(`${BASE}${route}`, { waitUntil: 'domcontentloaded', timeout: 25000 })
  await page.waitForLoadState('networkidle', { timeout: 12000 }).catch(() => {})
  const scan = await new AxeBuilder({ page }).withTags(TAGS).analyze()
  results.push({ route, violations: scan.violations })

  const bySeverity = (level) => scan.violations.filter((v) => v.impact === level).length
  const serious = bySeverity('critical') + bySeverity('serious')
  const other = scan.violations.length - serious
  console.log(
    `${(serious ? 'FAIL' : 'ok').padEnd(5)} ${route.padEnd(32)} ` +
      `serious+critical=${String(serious).padStart(2)} moderate+minor=${String(other).padStart(2)}`,
  )
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

writeFileSync(process.env.SWEEP_OUT ?? 'a11y-sweep.json', JSON.stringify(rules, null, 2), 'utf-8')

console.log('\n--- distinct rules violated, worst first ---')
for (const r of rules) {
  console.log(
    `  [${(r.impact ?? 'n/a').padEnd(8)}] ${r.id.padEnd(28)} ${r.nodes} node(s) on ${r.routes.length} screen(s)`,
  )
  console.log(`      ${r.help}`)
  if (r.sample) console.log(`      e.g. ${r.sample.replace(/\s+/g, ' ')}`)
}

const blocking = rules.filter((r) => r.impact === 'critical' || r.impact === 'serious')
console.log(
  `\n${results.length} screens | ${rules.length} distinct rules | ` +
    `${blocking.length} serious or critical`,
)
if (blocking.length) process.exitCode = 1
