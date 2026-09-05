/**
 * Data sweep: does each list screen actually put the database on the page?
 *
 * The browser sweep beside this one proves a route renders without crashing.
 * That is a weaker claim than it sounds: a screen that queries the wrong path,
 * filters everything out, or silently swallows its error renders a perfectly
 * calm empty state and passes. Against a nearly empty database the two
 * outcomes are indistinguishable, which is why this could not be written until
 * there was demonstration data to expect.
 *
 * So this asks the harder question. For each list screen it waits for the table
 * to settle and counts body rows, then compares that against what the API
 * returns for the same resource. Three failures are worth separating:
 *
 *   EMPTY    the API has rows and the screen shows none - a broken query,
 *            a filter defaulting wrongly, or a response shape mismatch
 *   MISMATCH both have rows but not the same number - usually a client-side
 *            filter that should have been sent to the server
 *   NO-DATA  neither has rows - not a defect, but not evidence of anything
 *            either, and it is reported so it cannot be mistaken for a pass
 */

import { chromium } from '@playwright/test'
import { writeFileSync } from 'node:fs'

import { API_URL as API, BASE_URL as BASE } from '../lib/env.mjs'
import { readyAdminToken, signInThroughForm } from '../lib/session.mjs'

/** route -> the API list endpoint whose rows it is supposed to be showing. */
const SCREENS = [
  ['/items', '/products/items'],
  ['/warehouses', '/warehouses'],
  ['/warehouses/locations', '/warehouses/locations'],
  ['/masters', '/products/categories'],
  ['/customers', '/customers'],
  ['/dealers', '/customers/dealers'],
  ['/vendors', '/vendors'],
  ['/transporters', '/transporters'],
  ['/bom', '/bom'],
  ['/routing', '/routing'],
  ['/inventory', '/inventory/balances'],
  ['/inventory/ledger', '/inventory/ledger'],
  ['/inventory/reservations', '/allocation/reservations'],
  ['/sales-orders', '/sales'],
  ['/mrp', '/mrp/runs'],
  ['/procurement/requisitions', '/procurement/purchase-requisitions'],
  ['/procurement/purchase-orders', '/procurement/purchase-orders'],
  ['/procurement/goods-receipts', '/procurement/goods-receipts'],
  ['/procurement/rfqs', '/procurement/rfqs'],
  ['/procurement/quotations', '/procurement/quotations'],
  ['/procurement/purchase-returns', '/procurement/purchase-returns'],
  ['/production', '/production'],
  ['/production/work-orders', '/production/work-orders'],
  ['/assembly', '/assembly'],
  ['/job-work', '/job-work'],
  // The quality screen opens on its PENDING queue tab, which is a different
  // endpoint from the full inspection history on the next tab. Comparing it
  // against /quality/inspections counts closed inspections the queue is right
  // not to show.
  ['/quality', '/quality/pending'],
  ['/quality/templates', '/quality/templates'],
  ['/packing', '/packing'],
  ['/dispatch', '/dispatch'],
  ['/finance/vendor-invoices', '/finance/vendor-invoices'],
  ['/finance/payments', '/finance/payments'],
  ['/finance/accounts', '/finance/accounts'],
  ['/documents', '/documents'],
  ['/admin/users', '/users'],
]

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } })
const page = await context.newPage()

// --- log in, and keep the token so the API can be asked the same question ---
// The seeded administrator is forced through a password change before it can
// reach any screen; `readyAdminToken` completes that through the API on a
// fresh database, and is one successful login otherwise.
const token = await readyAdminToken()
await signInThroughForm(page)

async function apiCount(path) {
  const response = await fetch(`${API}${path}?page=1&limit=100`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  if (!response.ok) return { count: null, status: response.status }
  const body = await response.json()
  const data = body?.data
  return { count: Array.isArray(data) ? data.length : null, status: 200 }
}

const results = []

for (const [route, endpoint] of SCREENS) {
  const httpFailures = []
  const onResponse = (r) => {
    const u = new URL(r.url())
    if (u.pathname.startsWith('/api/') && r.status() >= 400 && r.status() !== 401) {
      httpFailures.push(`${r.status()} ${r.request().method()} ${u.pathname}`)
    }
  }
  page.on('response', onResponse)

  await page.goto(`${BASE}${route}`, { waitUntil: 'domcontentloaded', timeout: 25000 })
  await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {})

  // Count real body rows, excluding the single row a table renders to say it is
  // empty or loading - that row is the absence of data, not an instance of it.
  const rows = await page
    .evaluate(() => {
      const bodies = Array.from(document.querySelectorAll('table tbody'))
      let total = 0
      for (const body of bodies) {
        for (const tr of Array.from(body.querySelectorAll(':scope > tr'))) {
          const cells = tr.querySelectorAll('td')
          if (cells.length <= 1) continue // "No results" / skeleton spanner
          total += 1
        }
      }
      return total
    })
    .catch(() => 0)

  page.off('response', onResponse)
  const { count: apiRows, status } = await apiCount(endpoint)

  let verdict
  if (httpFailures.length) verdict = 'HTTP'
  else if (status !== 200) verdict = `API-${status}`
  else if ((apiRows ?? 0) === 0 && rows === 0) verdict = 'NO-DATA'
  else if (rows === 0) verdict = 'EMPTY'
  else if (apiRows !== null && rows !== Math.min(apiRows, rows > 50 ? rows : 50) && rows !== apiRows)
    verdict = 'MISMATCH'
  else verdict = 'ok'

  results.push({ route, endpoint, uiRows: rows, apiRows, status, httpFailures, verdict })
  console.log(
    `${verdict.padEnd(9)} ${route.padEnd(32)} ui=${String(rows).padStart(3)} api=${String(apiRows ?? '-').padStart(3)}` +
      (httpFailures.length ? `  ${httpFailures[0]}` : ''),
  )
}

writeFileSync(process.env.SWEEP_OUT ?? 'data-sweep.json', JSON.stringify(results, null, 2), 'utf-8')

const bad = results.filter((r) => !['ok', 'NO-DATA'].includes(r.verdict))
const noData = results.filter((r) => r.verdict === 'NO-DATA')
console.log(
  `\n${results.length} screens | ok=${results.filter((r) => r.verdict === 'ok').length} ` +
    `problems=${bad.length} no-data=${noData.length}`,
)
if (noData.length) console.log(`no data to prove anything with: ${noData.map((r) => r.route).join(', ')}`)

await browser.close()
