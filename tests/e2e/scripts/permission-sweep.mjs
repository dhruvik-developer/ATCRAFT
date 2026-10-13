/**
 * Permission sweep: does the UI actually behave for someone who is NOT an
 * administrator?
 *
 * Every other check in this directory signed in as the seeded administrator,
 * which is a superuser - and a superuser passes every permission check by
 * construction. So the browser sweep, the data sweep and the end-to-end journey
 * all exercised the "sees everything" path and none of them touched the gates.
 *
 * That is the wrong way round for a system where the recent defects were all
 * authorisation defects: five screens gating an action on a right their endpoint
 * does not enforce, and a route gated on nothing at all. Those fail in *both*
 * directions, and the direction an administrator can never see is the one that
 * hides a control from the person entitled to use it.
 *
 * This signs in as a restricted user and checks two things per route:
 *
 *   FORBIDDEN-SHOWN   the route renders content the user has no right to - the
 *                     dangerous direction, and the one that ends in a 403
 *   DENIED            the route is refused, which for a right they lack is the
 *                     correct outcome and is reported, not failed
 *
 * It also asserts the converse: a route the user IS entitled to must render.
 */

import { chromium } from '@playwright/test'
import { writeFileSync } from 'node:fs'

import { BASE_URL as BASE } from '../lib/env.mjs'

// A restricted account, not the administrator: an administrator passes every
// gate by construction. Create it with `must_change_password: false`, or the
// login below parks on /change-password instead of reaching a screen.
const EMAIL = process.env.SWEEP_USER ?? 'storekeeper@atcraft.example'
const PASSWORD = process.env.SWEEP_PASSWORD ?? 'StorePass!Atcraft2026'

/**
 * What this user actually holds. Anything else must be refused.
 * Keep in step with the account the caller creates.
 */
const GRANTED = new Set([
  'dashboard.view',
  'inventory.view',
  'item.view',
  'warehouse.view',
  'sales_order.view',
])

/** route -> the permission its route guard declares (null = ungated by design). */
const ROUTES = [
  ['/', 'dashboard.view'],
  ['/items', 'item.view'],
  ['/warehouses', 'warehouse.view'],
  ['/inventory', 'inventory.view'],
  ['/inventory/movements', 'inventory.view'],
  ['/sales-orders', 'sales_order.view'],
  ['/profile', null],
  // --- everything below is a right this user does not hold ---
  ['/customers', 'customer.view'],
  ['/vendors', 'vendor.view'],
  ['/transporters', 'transporter.view'],
  ['/dealers', 'dealer.view'],
  ['/bom', 'bom.view'],
  ['/routing', 'routing.view'],
  ['/masters', 'category.view'],
  ['/mrp', 'mrp.view'],
  ['/warehouses/locations', 'location.view'],
  ['/inventory/ledger', 'inventory.ledger_view'],
  ['/inventory/reservations', 'reservation.view'],
  ['/procurement/requisitions', 'purchase_requisition.view'],
  ['/procurement/purchase-orders', 'purchase_order.view'],
  ['/procurement/goods-receipts', 'grn.view'],
  ['/procurement/rfqs', 'rfq.view'],
  ['/procurement/quotations', 'quotation.view'],
  ['/procurement/purchase-returns', 'purchase_return.view'],
  ['/production', 'production_order.view'],
  ['/production/work-orders', 'work_order.view'],
  ['/assembly', 'assembly_order.view'],
  ['/job-work', 'job_work.view'],
  ['/quality', 'qc.view'],
  ['/quality/templates', 'qc.view'],
  ['/packing', 'packing.view'],
  ['/dispatch', 'dispatch.view'],
  ['/finance', 'finance.view'],
  ['/finance/payments', 'payment.view'],
  ['/finance/accounts', 'ledger.view'],
  ['/finance/sales-invoices', 'sales_invoice.view'],
  ['/finance/vendor-invoices', 'vendor_invoice.view'],
  ['/documents', 'document.view'],
  ['/reports', 'report.view'],
  ['/audit', 'audit_log.view'],
  ['/admin/users', 'user.view'],
  ['/admin/organisation', 'company.view'],
  ['/notifications', 'notification.view'],
]

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()

await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' })
await page.getByRole('textbox', { name: /email/i }).fill(EMAIL)
await page.getByRole('textbox', { name: /password/i }).fill(PASSWORD)
await page.getByRole('button', { name: /sign in|log ?in/i }).click()
await page.waitForURL((u) => !u.pathname.includes('/login'), { timeout: 20000 })
console.log(`signed in as ${EMAIL} (not a superuser)\n`)

// The sidebar is the first place over-exposure shows: it hides what a user
// cannot use, so an entry here for a right they lack is a visible promise the
// API will refuse.
const navLinks = await page.evaluate(() =>
  Array.from(document.querySelectorAll('nav a[href]')).map((a) => a.getAttribute('href') ?? ''),
)

const results = []

for (const [route, permission] of ROUTES) {
  const entitled = permission === null || GRANTED.has(permission)
  const httpForbidden = []
  const onResponse = (r) => {
    if (new URL(r.url()).pathname.startsWith('/api/') && r.status() === 403) {
      httpForbidden.push(`${r.request().method()} ${new URL(r.url()).pathname}`)
    }
  }
  page.on('response', onResponse)

  await page.goto(`${BASE}${route}`, { waitUntil: 'domcontentloaded', timeout: 25000 })
  await page.waitForLoadState('networkidle', { timeout: 12000 }).catch(() => {})
  const body = await page.evaluate(() => document.body.innerText).catch(() => '')
  page.off('response', onResponse)

  const denied = /do not have|not authorised|not authorized|permission to|no access/i.test(body)
  const inNav = navLinks.includes(route)

  let verdict
  if (entitled) {
    verdict = denied ? 'WRONGLY-DENIED' : httpForbidden.length ? 'ENTITLED-BUT-403' : 'ok'
  } else if (!denied) {
    // Rendered a page they hold no right for. Whether it then 403s on its data
    // is beside the point - the route guard let them in.
    verdict = 'FORBIDDEN-SHOWN'
  } else {
    verdict = inNav ? 'DENIED-BUT-IN-NAV' : 'denied'
  }

  results.push({ route, permission, entitled, denied, inNav, httpForbidden, verdict })
  const flag = ['ok', 'denied'].includes(verdict) ? ' ' : '!'
  console.log(
    `${flag} ${verdict.padEnd(18)} ${route.padEnd(32)} needs=${permission ?? '(none)'}`,
  )
}

writeFileSync(
  process.env.SWEEP_OUT ?? 'permission-sweep.json',
  JSON.stringify({ user: EMAIL, granted: [...GRANTED], navLinks, results }, null, 2),
  'utf-8',
)

const problems = results.filter((r) => !['ok', 'denied'].includes(r.verdict))
console.log(`\n${results.length} routes | problems=${problems.length}`)
for (const p of problems) console.log(`  ${p.verdict}: ${p.route} (needs ${p.permission})`)

await browser.close()
