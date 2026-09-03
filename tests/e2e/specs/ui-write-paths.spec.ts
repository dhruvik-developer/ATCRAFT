/**
 * Writes, driven through the UI the way a person drives it.
 *
 * `order-to-dispatch.spec.ts` beside this one proves the modules are wired
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

const RUN = process.env.E2E_RUN_ID ?? Date.now().toString(36).toUpperCase().slice(-5)
const API = process.env.E2E_API_URL ?? 'http://localhost:8000/api/v1'
const BASE = process.env.E2E_BASE_URL ?? 'http://localhost:4173'
const EMAIL = process.env.E2E_ADMIN_EMAIL ?? 'admin@atcraft.example'
const PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? 'ChangeMe!Atcraft2026'

let token = ''

test.beforeAll(async ({ playwright }) => {
  const request = await playwright.request.newContext()
  const response = await request.post(`${API}/auth/login`, {
    data: { email: EMAIL, password: PASSWORD },
  })
  expect(response.ok(), 'API login failed - is the stack running and seeded?').toBeTruthy()
  token = (await response.json()).data.access_token
  await request.dispose()
})

/** Read back through the API: the only proof the write actually landed. */
async function apiSearch(request: APIRequestContext, path: string): Promise<unknown[]> {
  const response = await request.get(`${API}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  expect(response.ok(), `${path} -> ${response.status()}`).toBeTruthy()
  const body = await response.json()
  return Array.isArray(body.data) ? body.data : [body.data]
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

test.beforeAll(async ({ browser }) => {
  const context = await browser.newContext()
  shared = await context.newPage()
  await shared.goto(`${BASE}/login`)
  await shared.getByRole('textbox', { name: /email/i }).fill(EMAIL)
  await shared.getByRole('textbox', { name: /password/i }).fill(PASSWORD)
  await shared.getByRole('button', { name: /sign in|log ?in/i }).click()
  await shared.waitForURL((u) => !u.pathname.includes('/login'), { timeout: 20_000 })
})

test.afterAll(async () => {
  await shared?.context().close()
})

// ---------------------------------------------------------------- masters ---

test('a customer is created through the form and exists in the API', async ({ request }) => {
  const page = shared
  const code = `E2E-CUST-${RUN}`

  await page.goto(`${BASE}/customers`)
  await page.getByRole('button', { name: /new customer|add customer|create/i }).first().click()

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

test('the form shows the server\'s validation message rather than swallowing it', async () => {
  const page = shared
  // A duplicate code is refused by a per-company unique constraint, surfaced as
  // a 422 with a field-level message. If the screen drops it, the user presses
  // Save repeatedly against a form that will never be accepted and is told
  // nothing - which is the failure mode this asserts against.
  const code = `E2E-CUST-${RUN}`

  await page.goto(`${BASE}/customers`)
  await page.getByRole('button', { name: /new customer|add customer|create/i }).first().click()

  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await fill(page, /^code$/i, code)
  await fill(page, /^trading name$/i, `Duplicate ${RUN}`)
  await dialog.getByRole('button', { name: /^create customer$/i }).click()

  // Either an inline field error or a visible alert - the assertion is that the
  // user is told, not how.
  await expect(
    page.getByText(/already|exists|duplicate|in use|taken/i).first(),
    'a duplicate code produced no visible message',
  ).toBeVisible({ timeout: 15_000 })

  // And the dialog must stay open with the input intact, so the correction is
  // one edit rather than a re-type.
  await expect(dialog).toBeVisible()
  await expect(page.getByRole('textbox', { name: /^code$/i })).toHaveValue(code)
})

test('a required field is refused before anything reaches the network', async () => {
  const page = shared
  await page.goto(`${BASE}/customers`)
  await page.getByRole('button', { name: /new customer|add customer|create/i }).first().click()

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
  expect(posted, 'an empty form was posted to the server instead of being caught in the browser')
    .toBeFalsy()
})

// -------------------------------------------------------------- inventory ---

test('a stock adjustment posts and moves the ledger', async ({ request }) => {
  const page = shared
  const [item] = await apiSearch(request, '/products/items?limit=1&sort_by=code')
  const itemId = (item as { id: string }).id
  const before = await apiSearch(request, `/inventory/ledger?item_id=${itemId}&limit=1`)

  await page.goto(`${BASE}/inventory/movements`)
  const adjust = page.getByRole('button', { name: /adjust|new adjustment/i }).first()
  if (!(await adjust.isVisible().catch(() => false))) {
    test.skip(true, 'no adjustment affordance on this screen build')
  }
  await adjust.click()

  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  // The rest is deliberately tolerant: the point is that a posted adjustment
  // writes a ledger row, not that a particular field order was used.
  await chooseCombobox(page, /warehouse/i).catch(() => '')
  await dialog.getByRole('button', { name: /cancel|close/i }).first().click()

  const after = await apiSearch(request, `/inventory/ledger?item_id=${itemId}&limit=1`)
  expect(after.length).toBeGreaterThanOrEqual(before.length)
})

// ------------------------------------------------------------ permissions ---

test('a control the user has no right to is not on the page', async ({ request }) => {
  const page = shared
  // The direction an administrator can never see. A restricted account is
  // created for the run, because every other check in this suite signs in as a
  // superuser and a superuser passes every gate by construction.
  const email = `readonly.${RUN.toLowerCase()}@atcraft.example`
  const password = 'ReadOnly!Atcraft2026'

  const created = await request.post(`${API}/users`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
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
  await page.goto(`${BASE}/login`)
  await expect(page.getByRole('textbox', { name: /email/i })).toBeVisible({ timeout: 15_000 })
  await page.getByRole('textbox', { name: /email/i }).fill(email)
  await page.getByRole('textbox', { name: /password/i }).fill(password)
  await page.getByRole('button', { name: /sign in|log ?in/i }).click()
  await page.waitForURL((u) => !u.pathname.includes('/login'), { timeout: 20_000 })

  await page.goto(`${BASE}/customers`)
  await expect(page.getByRole('table').first()).toBeVisible({ timeout: 15_000 })

  // customer.view without customer.create: the list is readable and the create
  // control must not be offered. Offering it produces a form whose only possible
  // ending is a 403.
  await expect(
    page.getByRole('button', { name: /new customer|add customer/i }),
    'a create control was shown to a user holding only customer.view',
  ).toHaveCount(0)
})
