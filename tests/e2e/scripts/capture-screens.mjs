/**
 * Move to a screen WITHOUT reloading the page.
 *
 * This is the whole reason the capture works, and it took three failed runs to
 * arrive at. Every screen was originally a `page.goto`, and a full page load
 * restores the session from the refresh cookie - which rotates. Fifty of those
 * back to back is fifty rotations, and the moment two overlap the family is
 * revoked as a replay and the application lands on /login. Two earlier runs
 * reported "53 captured, 0 skipped" while more than half the files were
 * byte-identical pictures of the sign-in screen.
 *
 * One page load, then client-side routing, is also simply what a person does:
 * they click the menu, they do not retype the address.
 *
 * React Router listens to `popstate`, so pushing the entry and dispatching the
 * event is a real navigation to it - the URL, the route match and the screen
 * all follow.
 */
async function navigate(route) {
  await page.evaluate((path) => {
    window.history.pushState({}, '', path)
    window.dispatchEvent(new PopStateEvent('popstate'))
  }, route)
  try {
    await page.locator('main').waitFor({ timeout: 30_000 })
  } catch {
    // Two screens - the prices tab and the item detail - occasionally lose the
    // race between the popstate and their lazy chunk, and the route then never
    // mounts. Pushing the entry again after the chunk has had time to arrive
    // costs a second and is the difference between 51 screens and 53.
    await page.waitForTimeout(2000)
    await page.evaluate((path) => {
      window.history.pushState({}, '', path)
      window.dispatchEvent(new PopStateEvent('popstate'))
    }, route)
    await page.locator('main').waitFor({ timeout: 30_000 })
  }
}

/** The sidebar's collapse control exists only inside the application shell. */
async function isInApp() {
  return (await page.getByRole('button', { name: 'Collapse' }).count()) > 0
}

/**
 * Capture every screen of the running application, for the client handbook.
 *
 * Not a test - a photographer. It signs in once, walks every route, waits for
 * the screen to actually finish (a lazy chunk, then its first query), and writes
 * a PNG per screen.
 *
 * Two things it deliberately does NOT do:
 *
 * - It never waits for `networkidle`. The shell polls unread notifications on an
 *   interval, so this application is never network-idle and that wait can only
 *   time out.
 * - It does not screenshot the full scrollable page by default. A handbook page
 *   holds one image; a 4000px-tall capture of a 200-row table is unreadable at
 *   that size. The viewport is a generous desktop and that is what is captured,
 *   which is also what the reader will see.
 *
 * Detail routes carry an id, so the ids are discovered from the API first rather
 * than hardcoded - a hardcoded id is a screenshot that silently becomes a "not
 * found" page the day the demo data is reloaded.
 *
 *   E2E_BASE_URL=http://127.0.0.1 node scripts/capture-screens.mjs
 */

import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { chromium } from '@playwright/test'

import { ADMIN_EMAIL, ADMIN_PASSWORD, BASE_URL } from '../lib/env.mjs'

const OUT = fileURLToPath(new URL('../../../docs/handbook/screens/', import.meta.url))

/** A desktop a client would recognise. Tall enough to hold a table without scrolling. */
const VIEWPORT = { width: 1600, height: 1000 }

/**
 * Every screen, in the order the handbook reads.
 *
 * `settle` is extra milliseconds for screens that draw a chart or run a heavier
 * query; `action` is an optional interaction to capture a dialog rather than the
 * list behind it, which is how "how do I create one" gets a picture.
 */
const SCREENS = [
  { file: '01-dashboard', route: '/', title: 'Dashboard' },

  // --- masters ------------------------------------------------------------
  { file: '10-masters-categories', route: '/masters?tab=categories', title: 'Item categories' },
  { file: '11-masters-units', route: '/masters?tab=units', title: 'Units of measure' },
  { file: '12-masters-conversions', route: '/masters?tab=conversions', title: 'Unit conversions' },
  { file: '13-masters-attributes', route: '/masters?tab=attributes', title: 'Finishes, sizes and colours' },
  {
    file: '14-masters-attribute-new',
    route: '/masters?tab=attributes',
    title: 'Adding a finish',
    action: { click: 'New value', dialog: true },
  },
  { file: '16-masters-brands', route: '/masters?tab=brands', title: 'Brands' },
  { file: '17-masters-manufacturers', route: '/masters?tab=manufacturers', title: 'Manufacturers' },
  { file: '15-masters-prices', route: '/masters?tab=prices', title: 'Price lists' },

  // --- engineering --------------------------------------------------------
  { file: '20-items', route: '/items', title: 'Item master' },
  { file: '21-item-new', route: '/items', title: 'Creating an item', action: { click: 'New item', dialog: true } },
  { file: '22-item-detail', route: '@item', title: 'An item in detail' },
  { file: '23-bom', route: '/bom', title: 'Bills of material' },
  { file: '24-bom-detail', route: '@bom', title: 'A bill of material' },
  { file: '25-routing', route: '/routing', title: 'Routings, work centres and machines' },

  // --- sales --------------------------------------------------------------
  { file: '30-sales-orders', route: '/sales-orders', title: 'Sales orders' },
  { file: '31-sales-order-detail', route: '@salesOrder', title: 'A sales order' },
  { file: '32-customers', route: '/customers', title: 'Customers' },
  { file: '33-dealers', route: '/dealers', title: 'Dealers' },
  { file: '34-mrp', route: '/mrp', title: 'Material planning' },

  // --- procurement --------------------------------------------------------
  { file: '40-requisitions', route: '/procurement/requisitions', title: 'Purchase requisitions' },
  { file: '41-rfqs', route: '/procurement/rfqs', title: 'Requests for quotation' },
  { file: '42-quotations', route: '/procurement/quotations', title: 'Vendor quotations' },
  { file: '43-purchase-orders', route: '/procurement/purchase-orders', title: 'Purchase orders' },
  { file: '44-purchase-order-detail', route: '@purchaseOrder', title: 'A purchase order' },
  { file: '45-goods-receipts', route: '/procurement/goods-receipts', title: 'Goods receipts' },
  { file: '46-purchase-returns', route: '/procurement/purchase-returns', title: 'Purchase returns' },
  { file: '47-vendors', route: '/vendors', title: 'Vendors' },
  { file: '48-transporters', route: '/transporters', title: 'Transporters' },

  // --- production ---------------------------------------------------------
  { file: '50-production', route: '/production', title: 'Production orders' },
  { file: '51-production-detail', route: '@production', title: 'A production order' },
  { file: '52a-shift-roster', route: '/production/shift-roster', title: 'Shift roster' },
  { file: '52-work-orders', route: '/production/work-orders', title: 'Work orders' },
  { file: '53-assembly', route: '/assembly', title: 'Assembly' },
  { file: '54-job-work', route: '/job-work', title: 'Job work' },

  // --- quality, packing, dispatch ----------------------------------------
  { file: '60-quality', route: '/quality', title: 'Quality inspections' },
  { file: '61-quality-templates', route: '/quality/templates', title: 'Inspection check sheets' },
  { file: '62-packing', route: '/packing', title: 'Packing' },
  { file: '63-dispatch', route: '/dispatch', title: 'Dispatch' },

  // --- inventory ----------------------------------------------------------
  { file: '70-inventory', route: '/inventory', title: 'Stock balances' },
  { file: '71-inventory-ledger', route: '/inventory/ledger', title: 'Stock ledger' },
  { file: '72-reservations', route: '/inventory/reservations', title: 'Reservations' },
  { file: '73-movements', route: '/inventory/movements', title: 'Transfers, adjustments and counts' },
  { file: '74-warehouses', route: '/warehouses', title: 'Warehouses' },
  { file: '75-locations', route: '/warehouses/locations', title: 'Storage locations' },

  // --- money and evidence -------------------------------------------------
  { file: '80-finance', route: '/finance', title: 'Finance overview', settle: 2500 },
  { file: '81-vendor-invoices', route: '/finance/vendor-invoices', title: 'Vendor invoices' },
  { file: '82-payments', route: '/finance/payments', title: 'Payments' },
  { file: '83-accounts', route: '/finance/accounts', title: 'Chart of accounts' },
  { file: '84-reports', route: '/reports', title: 'Reports' },
  { file: '85-audit', route: '/audit', title: 'Audit trail' },
  { file: '86-documents', route: '/documents', title: 'Documents' },

  // --- administration -----------------------------------------------------
  { file: '90-users', route: '/admin/users', title: 'Users and roles' },
  { file: '91-organisation', route: '/admin/organisation', title: 'Branches, plants and departments' },
  { file: '91b-cost-centres', route: '/admin/organisation?tab=cost-centres', title: 'Cost centres' },
  { file: '92-notifications', route: '/notifications', title: 'Notifications' },
  { file: '93-profile', route: '/profile', title: 'Your profile' },
]

/**
 * The first row's link on a list screen.
 *
 * Reached by opening the list and reading its links rather than by asking the
 * API: the application holds its access token in memory and sends it as a
 * bearer header, so a `fetch` from inside the page carries no credentials and
 * answers 401. Clicking through is also what a person does, which makes this
 * the same path the screenshot is meant to illustrate.
 */
async function firstRowHref(listRoute, prefix) {
  await navigate(listRoute)
  await page.waitForTimeout(2200)
  const hrefs = await page
    .locator(`main a[href^="${prefix}/"]`)
    .evaluateAll((links) => links.map((link) => link.getAttribute('href')))
  return hrefs.find(Boolean) ?? null
}

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 2 })
const page = await context.newPage()

// --- sign in, and capture the login screen on the way through --------------
await mkdir(OUT, { recursive: true })
/**
 * Sign in. Called again whenever the walk lands back on the login screen.
 *
 * It does land back there. An access token lives fifteen minutes and this walk
 * takes longer, and every screen here is a FULL page load rather than a click
 * through the app - so each one restores the session from the refresh cookie,
 * and refresh tokens rotate. Rather than reason about which of those races is
 * the one that bites, the walk simply notices it is on /login and signs in
 * again. A fresh sign-in issues a fresh token family, so it cannot compound.
 *
 * The first run of this missed it entirely: thirty-two of fifty-three
 * screenshots were the login page, all byte-identical, and the run reported
 * "53 captured, 0 skipped".
 */
async function signIn() {
  await page.goto(`${BASE_URL}/login`)
  await page.getByRole('button', { name: /sign in/i }).waitFor({ timeout: 30_000 })
  await page.getByLabel('Email').fill(ADMIN_EMAIL)
  await page.locator('input[type="password"]').first().fill(ADMIN_PASSWORD)
  await page.getByRole('button', { name: /sign in/i }).click()
  await page.waitForURL((url) => !url.pathname.includes('/login'), { timeout: 30_000 })
  await page.getByRole('radiogroup', { name: 'Theme' }).waitFor({ timeout: 30_000 })
}

/**
 * Wait until the page has decided what it is, then say which.
 *
 * The decision cannot be taken the moment navigation returns. On a full page
 * load this application renders the sign-in screen while it restores the
 * session from the refresh cookie, and only then redirects - so a check that
 * runs immediately sees the login form on EVERY screen. The first attempt at
 * this signed in again on all fifty-three and captured the flicker each time.
 *
 * So: race the two things that are mutually exclusive once the boot is over.
 * The sidebar's collapse control exists only inside the application shell; the
 * sign-in submit exists only on the login screen. The theme control is on both
 * and is useless here, which is worth saying because it is the obvious choice.
 */
async function settleAndCheckAuth() {
  const inApp = page.getByRole('button', { name: 'Collapse' })
  const signInButton = page.getByRole('button', { name: /^sign in$/i })

  await Promise.race([
    inApp.waitFor({ timeout: 30_000 }).catch(() => {}),
    signInButton.waitFor({ timeout: 30_000 }).catch(() => {}),
  ])

  return (await inApp.count()) > 0 ? 'in' : 'out'
}

await page.goto(`${BASE_URL}/login`)
await page.getByRole('button', { name: /sign in/i }).waitFor({ timeout: 30_000 })
await page.waitForTimeout(600)
await page.screenshot({ path: path.join(OUT, '00-login.png') })
console.log('  00-login')

await signIn()

// --- resolve the detail routes from real data ------------------------------
const details = {
  '@item': '/items',
  '@bom': '/bom',
  '@salesOrder': '/sales-orders',
  '@purchaseOrder': '/procurement/purchase-orders',
  '@production': '/production',
}
const resolved = {}
for (const [token, listRoute] of Object.entries(details)) {
  resolved[token] = await firstRowHref(listRoute, listRoute)
  if (!resolved[token]) console.log(`  (no row on ${listRoute} - that screen will be skipped)`)
}

// --- walk every screen ------------------------------------------------------
const captured = []
const skipped = []
let previousRoute = null

for (const screen of SCREENS) {
  const route = screen.route.startsWith('@') ? resolved[screen.route] : screen.route
  if (!route) {
    skipped.push(`${screen.file} (${screen.route}: no data)`)
    continue
  }

  try {
    // Navigating to the URL already open is not a navigation, so the screen
    // never remounts and a control the capture is waiting for is never
    // re-rendered. Two screens here deliberately revisit the previous route to
    // open a dialog on it, and both timed out until this reload was added.
    // A screen that opens a dialog on the route already showing needs the
    // route re-entered, so it goes via the dashboard rather than reloading.
    if (route === previousRoute) await navigate('/')
    await navigate(route)
    previousRoute = route

    if (!(await isInApp())) {
      console.log('  (signed out - signing in again)')
      await signIn()
      await navigate(route)
    }
    // The lazy chunk mounts, then the screen's first query paints. There is no
    // single signal for "done" that holds across 50 different screens, so this
    // is a settle time rather than a condition - and a generous one, because a
    // half-drawn table in a client handbook is worse than a slow capture.
    await page.waitForTimeout(screen.settle ?? 1800)

    if (screen.action?.click) {
      const button = page.getByRole('button', { name: screen.action.click }).first()
      await button.waitFor({ timeout: 40_000 })
      await button.click()
      if (screen.action.dialog) await page.getByRole('dialog').waitFor({ timeout: 20_000 })
      await page.waitForTimeout(1200)
    }

    await page.screenshot({ path: path.join(OUT, `${screen.file}.png`) })
    captured.push({ file: `${screen.file}.png`, route, title: screen.title })
    console.log(`  ${screen.file}`)

    if (screen.action?.dialog) {
      // Wait for it to actually be gone, not merely asked to go. Radix marks
      // the rest of the page aria-hidden while a modal is open, so `main` is
      // not *visible* until the close finishes - and the next screen's
      // `waitFor` then times out against a page that is perfectly fine. That
      // cost the two screens following a dialog on three separate runs.
      await page.keyboard.press('Escape')
      await page.getByRole('dialog').waitFor({ state: 'detached', timeout: 15_000 }).catch(() => {})
    }
  } catch (error) {
    skipped.push(`${screen.file} (${route}: ${String(error).split('\n')[0].slice(0, 100)})`)
    console.log(`  SKIPPED ${screen.file}`)
  }
}

await writeFile(path.join(OUT, 'index.json'), JSON.stringify({ captured, skipped }, null, 2))
await browser.close()

console.log(`\n${captured.length} screens captured, ${skipped.length} skipped`)
for (const s of skipped) console.log('  -', s)

// A screenshot of the login page is a silent failure: the file exists, the run
// reports success, and the handbook ships with the same picture on thirty
// pages. Identical byte lengths are the tell, so the run fails on them.
const { readdir, stat } = await import('node:fs/promises')
const sizes = new Map()
for (const file of (await readdir(OUT)).filter((f) => f.endsWith('.png') && f !== '00-login.png')) {
  const { size } = await stat(path.join(OUT, file))
  sizes.set(size, [...(sizes.get(size) ?? []), file])
}
const duplicates = [...sizes.values()].filter((files) => files.length > 2)
if (duplicates.length) {
  console.log('\nIDENTICAL CAPTURES - these are almost certainly the same page:')
  for (const files of duplicates) console.log(`  ${files.length}x  ${files.slice(0, 6).join(', ')}`)
  process.exit(1)
}
