/**
 * Writes, driven through the UI the way a person drives it.
 *
 * `order-to-receipt.spec.ts` beside this one proves the modules are wired
 * together, and does most of its work through the API on purpose - twelve
 * form-filling detours would make it slow and brittle without testing anything
 * the backend suites do not already cover.
 *
 * This file is the other half, and the half that was missing. Every screen sweep
 * so far has proved that lists *render*; none of them ever opened a dialog,
 * filled a field or pressed Save. So the create and edit paths across the whole
 * application - every dialog, every validation message, every optimistic cache
 * update - had never once been exercised by anything but a human, and not often
 * by one.
 *
 * What each case does is the same shape: open the screen, open the form, fill it
 * as a person would, submit, and then assert the record exists **through the
 * API**. Asserting on a toast proves the toast fired; asserting on the API
 * proves the write landed. Both are checked, because a write that succeeds
 * without telling the user is its own defect.
 *
 * Every record is suffixed with a per-run code so repeated runs do not collide
 * on the per-company uniqueness constraints, and so a failed run leaves evidence
 * behind rather than poisoning the next one.
 */

import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

import { API_URL, BASE_URL } from '../lib/env.mjs'
import { createNavigator } from '../lib/navigation.mjs'
import { readyAdminToken, signInThroughForm } from '../lib/session.mjs'

const RUN = process.env.E2E_RUN_ID ?? Date.now().toString(36).toUpperCase().slice(-5)

let token = ''

test.beforeAll(async () => {
  // Bootstraps the seeded account past its forced password change on a fresh
  // database, and is a single successful login on every run after that.
  token = await readyAdminToken()
})

/** Read back through the API: the only proof the write actually landed. */
async function apiSearch(request: APIRequestContext, path: string): Promise<unknown[]> {
  const response = await request.get(`${API_URL}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  expect(response.ok(), `${path} -> ${response.status()}`).toBeTruthy()
  const body = await response.json()
  return Array.isArray(body.data) ? body.data : [body.data]
}

function authHeaders(): Record<string, string> {
  return {
    Authorization: ['Bearer', token].join(' '),
    'Content-Type': 'application/json',
  }
}

async function apiCreate<T>(
  request: APIRequestContext,
  path: string,
  payload: unknown,
): Promise<T> {
  const response = await request.post(`${API_URL}${path}`, {
    headers: authHeaders(),
    data: payload,
  })
  const body = await response.json()
  expect(
    response.ok(),
    `POST ${path} -> ${response.status()}: ${JSON.stringify(body)}`,
  ).toBeTruthy()
  return body.data as T
}

/**
 * Fill a labelled field.
 *
 * By role, never by label text: several fields carry an adjacent control with a
 * matching accessible name - the password field and its "Show password" toggle
 * are the case that broke two specs - and `getByLabel` substring-matches both.
 */
async function fill(page: Page, name: RegExp, value: string): Promise<void> {
  await page.getByRole('textbox', { name }).fill(value)
}

async function chooseCombobox(page: Page, name: RegExp, optionIndex = 0): Promise<string> {
  const trigger = page.getByRole('combobox', { name })
  await trigger.click()
  const options = page.getByRole('option')
  await expect(options.first()).toBeVisible({ timeout: 10_000 })
  const chosen = await options.nth(optionIndex).textContent()
  await options.nth(optionIndex).click()
  return (chosen ?? '').trim()
}

test.describe.configure({ mode: 'serial' })

/**
 * One signed-in page, shared by every test in this file.
 *
 * Two real constraints force this, and both are the API behaving correctly:
 *
 * 1. **Refresh tokens rotate, and a replay burns the family.** `auth.setup.ts`
 *    writes a `storageState` that Playwright restores into each test's fresh
 *    context, so every test after the first presents an already-rotated token.
 *    That is a replay, the family is revoked - which is how a stolen refresh
 *    token is contained - and the test silently lands on the login screen with
 *    nothing to explain it.
 *
 * 2. **Login is rate limited to five attempts per email per five minutes**
 *    (`LOGIN_PER_EMAIL` in src/core/ratelimit.py). So "log in fresh per test"
 *    fixes the first problem and runs straight into the second: this file has
 *    five tests, and a second run inside the window fails at the third.
 *
 * One login, one page, tests in order. That is also what a real user session
 * looks like, which is worth something on its own.
 */
test.use({ storageState: { cookies: [], origins: [] } })

let shared: Page
let navigate: (route: string) => Promise<void>

test.beforeAll(async ({ browser }) => {
  // `baseURL` is passed explicitly: it is the same constant the config reads.
  const context = await browser.newContext({ baseURL: BASE_URL })
  shared = await context.newPage()
  const navigator = createNavigator(shared)
  await signInThroughForm(shared)
  await navigator.settle()
  navigate = navigator.navigate
})

test.afterAll(async () => {
  await shared?.context().close()
})

// ---------------------------------------------------------------- masters ---

test('a customer is created through the form and exists in the API', async ({ request }) => {
  const page = shared
  const code = `E2E-CUST-${RUN}`

  await navigate(`/customers`)
  await page
    .getByRole('button', { name: /new customer|add customer|create/i })
    .first()
    .click()

  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()

  // Exact field names, read off the rendered form rather than guessed:
  // /name/i alone matches both "Trading name" and "Legal name" and is a strict
  // mode violation, not a flake.
  await fill(page, /^code$/i, code)
  await fill(page, /^trading name$/i, `E2E Buyer ${RUN}`)

  await dialog.getByRole('button', { name: /^create customer$/i }).click()

  // The dialog closing is the screen's own claim that it worked. It is checked
  // first because a form that saves and stays open reads as a failure to the
  // person using it, whatever the server did.
  await expect(dialog).toBeHidden({ timeout: 15_000 })

  const found = await apiSearch(request, `/customers?search=${encodeURIComponent(code)}&limit=5`)
  expect(
    found.some((row) => (row as { code?: string }).code === code),
    `no customer with code ${code} came back from the API after the form said it saved`,
  ).toBeTruthy()
})

test("the form shows the server's validation message rather than swallowing it", async () => {
  const page = shared
  // A duplicate code is refused by a per-company unique constraint, surfaced as
  // a 409 with a safe message. If the screen drops it, the user presses
  // Save repeatedly against a form that will never be accepted and is told
  // nothing - which is the failure mode this asserts against.
  const code = `E2E-CUST-${RUN}`

  await navigate(`/customers`)
  await page
    .getByRole('button', { name: /new customer|add customer|create/i })
    .first()
    .click()

  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await fill(page, /^code$/i, code)
  await fill(page, /^trading name$/i, `Duplicate ${RUN}`)
  const duplicateResponse = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      new URL(response.url()).pathname === `${new URL(API_URL).pathname}/customers`,
  )
  await dialog.getByRole('button', { name: /^create customer$/i }).click()
  const response = await duplicateResponse
  expect(response.status(), 'Only a real duplicate refusal proves this scenario').toBe(409)
  const refused = await response.json()
  expect(refused.error.code).toBe('DUPLICATE_DOCUMENT')

  // Either an inline field error or a visible alert - the assertion is that the
  // user is told, not how.
  await expect(
    page.getByText(refused.error.message).first(),
    'a duplicate code produced no visible message',
  ).toBeVisible({ timeout: 15_000 })

  // And the dialog must stay open with the input intact, so the correction is
  // one edit rather than a re-type.
  await expect(dialog).toBeVisible()
  await expect(page.getByRole('textbox', { name: /^code$/i })).toHaveValue(code)

  await dialog.getByRole('button', { name: 'Close' }).click()
  await expect(dialog).toBeHidden()
})

test('a required field is refused before anything reaches the network', async () => {
  const page = shared
  await navigate(`/customers`)
  await page
    .getByRole('button', { name: /new customer|add customer|create/i })
    .first()
    .click()

  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()

  let posted = false
  page.on('request', (req) => {
    if (req.method() === 'POST' && req.url().includes('/customers')) posted = true
  })

  await dialog.getByRole('button', { name: /^create customer$/i }).click()
  await expect(page.getByText(/required|enter|choose|must/i).first()).toBeVisible({
    timeout: 10_000,
  })
  expect(
    posted,
    'an empty form was posted to the server instead of being caught in the browser',
  ).toBeFalsy()
})

// -------------------------------------------------------------- inventory ---

test('a stock adjustment posts and moves the ledger', async ({ request }) => {
  const page = shared

  // A fresh item, so the ledger is provably empty before and holds exactly the
  // one posted adjustment afterwards. The previous version of this test read
  // the first seeded item's ledger twice around a dialog it only ever
  // cancelled, and compared two `limit=1` reads - which can never differ.
  const [unit] = await apiSearch(request, '/products/uoms?limit=1')
  const code = `E2E-ADJ-${RUN}`
  const created = await request.post(`${API_URL}/products/items`, {
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    data: {
      code,
      name: `E2E Adjustment Item ${RUN}`,
      item_group: 'RAW_MATERIAL',
      base_uom_id: (unit as { id: string }).id,
      production_type: 'BUY',
      can_purchase: true,
      // Five units at this cost is well under the default adjustment approval threshold,
      // so the draft can be posted straight away without a second approver.
      standard_cost: '10.00',
    },
  })
  expect(created.ok(), `could not create the item to adjust: ${created.status()}`).toBeTruthy()
  const itemId = (await created.json()).data.id as string
  const reason = `E2E adjustment ${RUN}: five found on the shelf after the cycle count`

  expect(
    await apiSearch(request, `/inventory/ledger?item_id=${itemId}&limit=5`),
    'a freshly created item already has ledger history',
  ).toHaveLength(0)

  await navigate('/inventory/movements?tab=adjustments')

  // Not a skip. An administrator holds inventory.adjust, so a missing control
  // is a defect on the screen, and a skip would report it as nothing at all.
  const raise = page.getByRole('button', { name: 'New adjustment' })
  await expect(raise, 'the New adjustment control is not offered to an administrator').toBeVisible()
  await raise.click()

  const dialog = page.getByRole('dialog', { name: 'New stock adjustment' })
  await expect(dialog).toBeVisible()

  await chooseCombobox(page, /^warehouse$/i)
  await page.getByRole('combobox', { name: 'Reason code' }).click()
  await page.getByRole('option', { name: 'Correction' }).click()
  await fill(page, /^reason$/i, reason)

  // The item picker is a combobox with its own search box; the option carries
  // the code, which is how a storekeeper finds a part too.
  await page.getByRole('combobox', { name: 'Item, line 1' }).click()
  await page.getByRole('textbox', { name: 'Search items' }).fill(code)
  await page.getByRole('option', { name: code }).first().click()
  await fill(page, /^quantity$/i, '5')

  await dialog.getByRole('button', { name: 'Raise adjustment' }).click()
  await expect(dialog).toBeHidden({ timeout: 15_000 })

  // Raising it makes a draft; stock does not move until it is posted. Find the
  // row by its reason and post it through the row action, as a person would.
  await page.getByPlaceholder('Search adjustment code or reason').fill(reason)
  const row = page.getByRole('row').filter({ hasText: reason }).first()
  await expect(row).toBeVisible({ timeout: 15_000 })
  await row.getByRole('button', { name: 'Post' }).click()
  const confirm = page.getByRole('button', { name: 'Post adjustment' })
  await confirm.click()
  await expect(confirm).toBeHidden({ timeout: 15_000 })

  // The proof is on the ledger: exactly one entry, of the adjustment type, and
  // the balance it produced.
  const after = await apiSearch(request, `/inventory/ledger?item_id=${itemId}&limit=5`)
  expect(after, 'posting the adjustment wrote no ledger entry').toHaveLength(1)
  expect((after[0] as { transaction_type?: string }).transaction_type).toBe('ADJUSTMENT_INCREASE')

  const balances = await apiSearch(request, `/inventory/balances?item_id=${itemId}`)
  const available = (balances[0] as { available_quantity?: string } | undefined)?.available_quantity
  expect(Number(available ?? 0), 'the balance did not move by the posted quantity').toBe(5)
})

test('a dispatch invoice can be issued from its row and found in the invoice register', async ({
  request,
}) => {
  interface IdRecord {
    id: string
  }
  interface OrderRecord extends IdRecord {
    code: string
    lines: Array<{ id: string }>
  }
  interface DispatchRecord extends IdRecord {
    code: string
  }
  interface InvoiceRecord extends IdRecord {
    code: string
    status: string
    dispatch_id: string
  }

  const page = shared
  const [unit] = await apiSearch(request, '/products/uoms?limit=1')
  const [warehouse] = await apiSearch(request, '/warehouses?limit=1&warehouse_type=FINISHED_GOODS')
  expect(unit, 'the seeded UOM is missing').toBeTruthy()
  expect(warehouse, 'the seeded finished-goods warehouse is missing').toBeTruthy()

  const code = `E2E-INV-${RUN}`
  const item = await apiCreate<IdRecord>(request, '/products/items', {
    code: `ITEM-${code}`,
    name: `Invoice UI item ${RUN}`,
    item_group: 'FINISHED_GOOD',
    base_uom_id: (unit as { id: string }).id,
    production_type: 'MAKE',
    can_sell: true,
    sale_price: '100.00',
    gst_rate: '0',
  })
  const customer = await apiCreate<IdRecord>(request, '/customers', {
    code: `CUST-${code}`,
    name: `Invoice UI customer ${RUN}`,
    customer_type: 'DEALER',
  })

  const adjustment = await apiCreate<IdRecord>(request, '/inventory/adjustments', {
    warehouse_id: (warehouse as { id: string }).id,
    reason_code: 'OPENING',
    reason: `Initial stock for invoice UI journey ${RUN}`,
    lines: [{ item_id: item.id, quantity: '1', unit_cost: '10.00' }],
  })
  const adjustmentPosted = await request.post(
    `${API_URL}/inventory/adjustments/${adjustment.id}/post`,
    { headers: authHeaders() },
  )
  expect(adjustmentPosted.ok(), 'could not seed dispatchable stock').toBeTruthy()

  const requiredDate = new Date()
  requiredDate.setDate(requiredDate.getDate() + 14)
  const order = await apiCreate<OrderRecord>(request, '/sales', {
    customer_id: customer.id,
    warehouse_id: (warehouse as { id: string }).id,
    required_date: requiredDate.toISOString().slice(0, 10),
    lines: [{ line_number: 1, item_id: item.id, quantity: '1', unit_price: '100.00' }],
  })
  await apiCreate(request, `/sales/${order.id}/confirm`, {
    reason: `Confirm invoice UI journey ${RUN}`,
  })
  const dispatch = await apiCreate<DispatchRecord>(request, '/dispatch', {
    dispatch_type: 'SALES',
    warehouse_id: (warehouse as { id: string }).id,
    dispatch_date: new Date().toISOString().slice(0, 10),
    customer_id: customer.id,
    sales_order_id: order.id,
    lines: [
      {
        item_id: item.id,
        quantity: '1',
        sales_order_line_id: order.lines[0].id,
      },
    ],
  })
  await apiCreate(request, `/dispatch/${dispatch.id}/ready`, {
    reason: `Ready invoice UI journey ${RUN}`,
  })
  const dispatched = await request.post(`${API_URL}/dispatch/${dispatch.id}/dispatch`, {
    headers: authHeaders(),
    data: { reason: `Dispatch invoice UI journey ${RUN}` },
  })
  expect(dispatched.ok(), 'could not dispatch the prepared sales order').toBeTruthy()

  await navigate('/dispatch')
  await page.getByPlaceholder('Search dispatch code or tracking id...').fill(dispatch.code)
  const row = page.getByRole('row').filter({ hasText: dispatch.code })
  await expect(row).toBeVisible({ timeout: 15_000 })
  await row.getByRole('button', { name: `Actions for ${dispatch.code}` }).click()
  await page.getByRole('menuitem', { name: 'Raise invoice' }).click()

  const dialog = page.getByRole('dialog', { name: 'Raise sales invoice' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('Invoice UI customer', { exact: false })).toBeVisible({
    timeout: 15_000,
  })
  await dialog.getByRole('button', { name: 'Confirm and issue' }).click()
  await expect(dialog).toBeHidden({ timeout: 15_000 })

  const invoices = await apiSearch(
    request,
    `/finance/sales-invoices?dispatch_id=${dispatch.id}&limit=5`,
  )
  const invoice = invoices.find(
    (candidate) =>
      (candidate as InvoiceRecord).dispatch_id === dispatch.id &&
      (candidate as InvoiceRecord).status === 'ISSUED',
  ) as InvoiceRecord | undefined
  expect(invoice, 'the UI did not issue an invoice for the dispatch').toBeTruthy()

  await navigate('/finance/sales-invoices')
  await page.getByPlaceholder('Search invoice number or customer').fill(invoice!.code)
  await expect(page.getByRole('row').filter({ hasText: invoice!.code })).toBeVisible({
    timeout: 15_000,
  })
})

// ------------------------------------------------------------ permissions ---

test('a control the user has no right to is not on the page', async ({ request }) => {
  const page = shared
  // The direction an administrator can never see. A restricted account is
  // created for the run, because every other check in this suite signs in as a
  // superuser and a superuser passes every gate by construction.
  const email = `readonly.${RUN.toLowerCase()}@atcraft.example`
  const password = 'ReadOnly!Atcraft2026'

  const created = await request.post(`${API_URL}/users`, {
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    data: {
      email,
      full_name: `E2E Read Only ${RUN}`,
      password,
      must_change_password: false,
      extra_permissions: ['customer.view', 'dashboard.view'],
    },
  })
  expect(created.ok(), `could not create the restricted user: ${created.status()}`).toBeTruthy()

  // `beforeEach` has already signed this context in as the administrator, and
  // /login redirects an authenticated visitor straight back out. Drop the
  // session first: the whole point of this case is what a *different* user sees.
  await page.evaluate(() => {
    window.localStorage.clear()
    window.sessionStorage.clear()
  })
  await page.context().clearCookies()
  await page.goto(`/login`)
  await expect(page.getByRole('textbox', { name: /email/i })).toBeVisible({
    timeout: 15_000,
  })
  await page.getByRole('textbox', { name: /email/i }).fill(email)
  await page.getByRole('textbox', { name: /password/i }).fill(password)
  await page.getByRole('button', { name: /sign in|log ?in/i }).click()
  await page.waitForURL((u) => !u.pathname.includes('/login'), {
    timeout: 20_000,
  })

  await navigate(`/customers`)
  await expect(page.getByRole('table').first()).toBeVisible({
    timeout: 15_000,
  })

  // customer.view without customer.create: the list is readable and the create
  // control must not be offered. Offering it produces a form whose only possible
  // ending is a 403.
  await expect(
    page.getByRole('button', { name: /new customer|add customer/i }),
    'a create control was shown to a user holding only customer.view',
  ).toHaveCount(0)
})
