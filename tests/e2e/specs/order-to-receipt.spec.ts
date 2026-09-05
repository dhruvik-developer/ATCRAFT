import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

import { API_URL, BASE_URL } from '../lib/env.mjs'
import { readyAdminToken } from '../lib/session.mjs'

/**
 * The critical journey from master rules section 66, as far as material
 * arriving:
 *
 *   login → item → BOM → sales order → stock check → MRP → purchase order
 *         → goods receipt (stock moves) → audit trail
 *
 * The rest of that journey - production, QC, packing, dispatch - is NOT wired
 * together here. It was named in this file's header for a long time without a
 * single step being driven, which told a reader those modules were covered
 * when they were not. The name and this header now say what is covered.
 *
 * Master data is created through the API rather than by driving forms: this spec
 * exists to prove the modules are *wired together*, and twelve form-filling
 * detours would make it slow and brittle without testing anything the backend
 * suites do not already cover. The UI assertions are placed where a human
 * decision actually happens, and every one names the page it expects - a bare
 * "some level-1 heading is visible" passes on the login screen, whose heading
 * is "Sign in", which is exactly where a broken session lands.
 *
 * Each run uses a unique code suffix so repeated runs do not collide on the
 * per-company uniqueness constraints.
 */

const RUN = Date.now().toString(36).toUpperCase().slice(-6)
const AUTH_FILE = './.auth/admin.json'

interface Ids {
  finishedGoodId: string
  componentId: string
  rawMaterialId: string
  warehouseId: string
  customerId: string
  vendorId: string
  bomId: string
  salesOrderId: string
}

let ids: Partial<Ids> = {}
let token: string
/** A second actor, so vendor approval has a checker distinct from its maker. */
let buyerToken = ''

/**
 * One signed-in page, shared by every test in this file.
 *
 * `auth.setup.ts` saves a storage state whose only credential is the refresh
 * cookie, and refresh tokens rotate: the first context to present it gets a new
 * one and the file's copy is spent. Restoring the file into a fresh context per
 * test - which is what the `page` fixture does - makes the second test a
 * replay, the API revokes the whole family (correctly: that is how a stolen
 * token is contained), and the test silently lands on /login. So the file is
 * restored once, here, and the page is reused in order.
 */
let shared: Page

async function apiJson(
  request: APIRequestContext,
  method: 'get' | 'post' | 'patch',
  path: string,
  body?: unknown,
  /** Act as somebody other than the administrator. Used for maker-checker. */
  asToken?: string,
) {
  const response = await request[method](`${API_URL}${path}`, {
    headers: {
      Authorization: `Bearer ${asToken ?? token}`,
      'Content-Type': 'application/json',
    },
    ...(body ? { data: body } : {}),
  })
  const payload = await response.json().catch(() => ({}))
  expect(
    response.ok(),
    `${method.toUpperCase()} ${path} → ${response.status()} ${JSON.stringify(payload).slice(0, 400)}`,
  ).toBeTruthy()
  return payload.data
}

test.describe.configure({ mode: 'serial' })

test.beforeAll(async ({ browser, playwright }) => {
  // Bootstraps the seeded account past its forced password change on a fresh
  // database, and is a single successful login on every run after that.
  token = await readyAdminToken()

  const request = await playwright.request.newContext()

  // Maker-checker needs two people. The administrator creates a buyer with the
  // rights this journey uses, and `must_change_password: false` so the account
  // can sign in immediately - a seeded user is normally forced to change it
  // first, which would stop the login below.
  const buyerEmail = `buyer.${RUN.toLowerCase()}@atcraft.example`
  const buyerPassword = 'BuyerPass!Atcraft2026'
  const created = await request.post(`${API_URL}/users`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: {
      email: buyerEmail,
      full_name: `E2E Buyer ${RUN}`,
      password: buyerPassword,
      must_change_password: false,
      extra_permissions: ['vendor.create', 'vendor.view'],
    },
  })
  expect(
    created.ok(),
    `creating the buyer failed → ${created.status()} ${JSON.stringify(await created.json().catch(() => ({}))).slice(0, 300)}`,
  ).toBeTruthy()

  const buyerLogin = await request.post(`${API_URL}/auth/login`, {
    data: { email: buyerEmail, password: buyerPassword },
  })
  expect(buyerLogin.ok(), 'the buyer could not sign in').toBeTruthy()
  buyerToken = (await buyerLogin.json()).data.access_token

  await request.dispose()

  const context = await browser.newContext({ baseURL: BASE_URL, storageState: AUTH_FILE })
  shared = await context.newPage()
})

test.afterAll(async () => {
  await shared?.context().close()
})

test('01 — master data: warehouse, items, partners', async ({ request }) => {
  // Every item needs a base unit: `base_uom_id` is required, and omitting it is
  // a 422 rather than a defaulted NOS. The baseline seed loads the unit table,
  // so this resolves what is there instead of creating one.
  const units = await apiJson(request, 'get', '/products/uoms?limit=50')
  const baseUomId = (units.find((u) => u.code === 'NOS') ?? units[0])?.id
  expect(baseUomId, 'no unit of measure exists — run the seed first').toBeTruthy()

  const warehouses = await apiJson(request, 'get', '/warehouses?limit=1&warehouse_type=FINISHED_GOODS')
  ids.warehouseId =
    warehouses?.[0]?.id ??
    (
      await apiJson(request, 'post', '/warehouses', {
        code: `WH-E2E-${RUN}`,
        name: `E2E Finished Goods ${RUN}`,
        warehouse_type: 'FINISHED_GOODS',
      })
    ).id

  // A finished good that is sold and manufactured — and a component that is
  // BOTH purchased and used in the BOM. That overlap is the core requirement of
  // the item model (master rules section 1), so the journey exercises it.
  const finished = await apiJson(request, 'post', '/products/items', {
    code: `FG-LOCKKIT-${RUN}`,
    name: `Premium Lock Kit ${RUN}`,
    item_group: 'FINISHED_GOOD',
    base_uom_id: baseUomId,
    production_type: 'MAKE',
    can_sell: true,
    can_manufacture: true,
    can_be_bom_component: false,
    sale_price: '4500.00',
    lead_time_days: 5,
  })
  ids.finishedGoodId = finished.id

  const component = await apiJson(request, 'post', '/products/items', {
    code: `CMP-ROSE-${RUN}`,
    name: `Rose Handle ${RUN}`,
    item_group: 'COMPONENT',
    base_uom_id: baseUomId,
    production_type: 'BUY',
    can_purchase: true,
    can_sell: true, // also sold as a spare — must not conflict with BOM use
    can_be_bom_component: true,
    can_be_spare: true,
    standard_cost: '380.00',
    lead_time_days: 7,
    min_order_quantity: '50',
  })
  ids.componentId = component.id

  const raw = await apiJson(request, 'post', '/products/items', {
    code: `RM-SCREW-${RUN}`,
    name: `Screw M4 ${RUN}`,
    item_group: 'RAW_MATERIAL',
    base_uom_id: baseUomId,
    production_type: 'BUY',
    can_purchase: true,
    can_be_bom_component: true,
    standard_cost: '2.40',
    lead_time_days: 10,
  })
  ids.rawMaterialId = raw.id

  ids.customerId = (
    await apiJson(request, 'post', '/customers', {
      code: `CUST-E2E-${RUN}`,
      name: `E2E Dealer ${RUN}`,
      customer_type: 'DEALER',
    })
  ).id

  ids.vendorId = (
    await apiJson(
      request,
      'post',
      '/vendors',
      {
        code: `VEND-E2E-${RUN}`,
        name: `E2E Component Supplier ${RUN}`,
        vendor_types: ['COMPONENT_SUPPLIER'],
        lead_time_days: 7,
        // Approval additionally requires bank details and a tax identity -
        // `_assert_approvable` refuses without them, and an unapproved vendor
        // cannot take a purchase order. They are not decoration here.
        bank_account_name: `E2E Component Supplier ${RUN}`,
        bank_account_number: '50100123456789',
        bank_ifsc: 'HDFC0001234',
        msme_number: `UDYAM-GJ-01-${RUN}`,
      },
      buyerToken,
    )
  ).id

  // Maker-checker, and it is the business rule rather than a formality:
  // `REQUIRE_SEPARATE_APPROVER` refuses a vendor approved by whoever created it,
  // because approval is what unlocks payments to a bank account the same person
  // typed in. The buyer above created it; the administrator signs it off.
  await apiJson(request, 'post', `/vendors/${ids.vendorId}/approve`, {
    reason: 'Bank details verified against the cancelled cheque',
  })
})

test('02 — BOM is created and approved', async ({ request }) => {
  const page = shared
  const bom = await apiJson(request, 'post', '/bom', {
    item_id: ids.finishedGoodId,
    bom_type: 'PRODUCTION',
    output_quantity: '1',
    is_default: true,
    lines: [
      { line_number: 1, component_item_id: ids.componentId, quantity_per: '2' },
      { line_number: 2, component_item_id: ids.rawMaterialId, quantity_per: '8', scrap_percentage: '5' },
    ],
  })
  ids.bomId = bom.id

  // Submit before approve: BOM_LIFECYCLE goes DRAFT -> PENDING_APPROVAL ->
  // APPROVED, with no shortcut. Skipping the submit is a 422 that names the two
  // states it will accept, which is the state machine doing its job - a draft
  // nobody put forward has not been reviewed by anyone.
  //
  // `reason` is required on both, not optional: BomStatusChange declares it, and
  // every audited transition in this journey carries one so test 06 has
  // something to reconstruct.
  await apiJson(request, 'post', `/bom/${bom.id}/submit`, {
    reason: 'Structure complete, sent for engineering review',
  })
  await apiJson(request, 'post', `/bom/${bom.id}/approve`, {
    reason: 'Engineering sign-off for the end-to-end journey',
  })

  // The explosion result is what MRP and production will consume, so verify it
  // in the UI where a planner would actually read it.
  await page.goto(`/bom/${bom.id}`)
  // The heading is the BOM's own document number, not the item's - this is a
  // document page, and `BOM/00008` is what a planner quotes on the phone. The
  // item it belongs to is on the page, which is what actually needs proving:
  // that this BOM is the one just built, and that it reads as approved.
  await expect(page.getByRole('heading', { level: 1, name: bom.code })).toBeVisible()
  await expect(page.getByText(`FG-LOCKKIT-${RUN}`).first()).toBeVisible()
  await expect(page.getByText('Approved', { exact: false }).first()).toBeVisible()
})

test('03 — sales order confirms and reserves what stock exists', async ({ request }) => {
  const page = shared
  const order = await apiJson(request, 'post', '/sales', {
    customer_id: ids.customerId,
    warehouse_id: ids.warehouseId,
    required_date: inDays(21),
    lines: [{ line_number: 1, item_id: ids.finishedGoodId, quantity: '100', unit_price: '4500.00' }],
  })
  ids.salesOrderId = order.id

  await apiJson(request, 'post', `/sales/${order.id}/confirm`, {
    reason: 'Customer purchase order received',
  })

  // The page identifies itself by the order's document number. A redirect to
  // /login also renders a level-1 heading, so the name is the assertion.
  await page.goto(`/sales-orders/${order.id}`)
  await expect(page.getByRole('heading', { level: 1, name: order.code })).toBeVisible()

  // With no finished-goods stock, a shortfall is the CORRECT outcome — partial
  // allocation is normal (master rules section 72), not an error state.
  const allocation = await apiJson(request, 'get', `/sales/${order.id}/allocation-status`)
  expect(Array.isArray(allocation) ? allocation.length : allocation.lines.length).toBeGreaterThan(0)
})

test('04 — MRP consolidates demand and proposes purchases', async ({ request }) => {
  const page = shared
  const run = await apiJson(request, 'post', '/mrp/runs', {
    horizon_days: 60,
    warehouse_ids: [ids.warehouseId],
  })
  const runId = run.id ?? run.job_id

  // The run may be inline or dispatched to Celery. Either is correct; the client
  // polls rather than holding a request open (master rules section 14).
  const completed = await pollUntil(
    async () => apiJson(request, 'get', `/mrp/runs/${runId}`),
    (r) => r.status === 'COMPLETED' || r.status === 'FAILED',
    120_000,
  )
  expect(completed.status, `MRP run failed: ${completed.error_message ?? ''}`).toBe('COMPLETED')

  const suggestions = (await apiJson(request, 'get', `/mrp/runs/${runId}/suggestions?limit=200`)) ?? []
  const rows = Array.isArray(suggestions) ? suggestions : suggestions.data

  // 100 kits × 2 rose handles = 200, and the screw requirement must appear as a
  // SINGLE consolidated line, not one per BOM branch.
  const screwSuggestions = rows.filter((s: { item_code?: string }) =>
    s.item_code?.includes(`RM-SCREW-${RUN}`),
  )
  expect(screwSuggestions.length, 'common parts must be consolidated into one requirement').toBe(1)

  await page.goto(`/mrp/runs/${runId}`)
  await expect(page.getByRole('heading', { level: 1, name: completed.code })).toBeVisible()
})

test('05 — purchase order, receipt, and stock actually moves', async ({ request }) => {
  const po = await apiJson(request, 'post', '/procurement/purchase-orders', {
    vendor_id: ids.vendorId,
    warehouse_id: ids.warehouseId,
    expected_delivery_date: inDays(7),
    lines: [
      { line_number: 1, item_id: ids.componentId, quantity: '200', unit_price: '380.00', required_date: inDays(7) },
    ],
  })

  // Submit before approve, as with the BOM: PURCHASE_ORDER_FLOW goes
  // DRAFT -> PENDING_APPROVAL -> APPROVED. A draft nobody put forward has not
  // been reviewed, so there is nothing to approve.
  await apiJson(request, 'post', `/procurement/purchase-orders/${po.id}/submit`, {
    reason: 'Priced and sent for buying approval',
  })
  await apiJson(request, 'post', `/procurement/purchase-orders/${po.id}/approve`, {
    reason: 'Within the buyer authority limit',
  })
  await apiJson(request, 'post', `/procurement/purchase-orders/${po.id}/send`, {
    reason: 'Order released to the vendor',
  })

  const before = await availableQty(request, ids.componentId!, ids.warehouseId!)

  // No `warehouse_id` on the receipt: goods land where the purchase order said
  // they would, so the receipt takes the destination from the order rather than
  // letting the gate redirect a delivery. Sending one is rejected outright as an
  // extra input, which is the schema being strict on purpose.
  const grn = await apiJson(request, 'post', '/procurement/goods-receipts', {
    purchase_order_id: po.id,
    lines: [
      {
        line_number: 1,
        purchase_order_line_id: po.lines[0].id,
        received_quantity: '200',
        accepted_quantity: '200',
      },
    ],
  })
  await apiJson(request, 'post', `/procurement/goods-receipts/${grn.id}/post`, {})

  const after = await availableQty(request, ids.componentId!, ids.warehouseId!)
  expect(Number(after) - Number(before)).toBeCloseTo(200, 2)

  // Every movement must be explained by a ledger entry (master rules section 83).
  const ledger = await apiJson(
    request,
    'get',
    `/inventory/ledger?item_id=${ids.componentId}&limit=5`,
  )
  const entries = Array.isArray(ledger) ? ledger : ledger.data
  expect(entries[0].transaction_type).toBe('PURCHASE_RECEIPT')
  expect(entries[0].reason ?? '').not.toBe('')
})

test('06 — the audit trail reconstructs what happened', async ({ request }) => {
  const page = shared
  const history = await apiJson(
    request,
    'get',
    `/audit/entity/sales_order/${ids.salesOrderId}`,
  )
  const entries = Array.isArray(history) ? history : history.data
  expect(entries.length, 'a confirmed sales order must leave an audit trail').toBeGreaterThan(0)

  await page.goto('/audit')
  await expect(page.getByRole('heading', { level: 1, name: 'Audit trail' })).toBeVisible()
})

// --- helpers -----------------------------------------------------------------

function inDays(days: number): string {
  const date = new Date()
  date.setDate(date.getDate() + days)
  return date.toISOString().slice(0, 10)
}

async function availableQty(
  request: APIRequestContext,
  itemId: string,
  warehouseId: string,
): Promise<string> {
  const balances = await apiJson(
    request,
    'get',
    `/inventory/balances?item_id=${itemId}&warehouse_id=${warehouseId}`,
  )
  const rows = Array.isArray(balances) ? balances : balances.data
  return rows[0]?.available_quantity ?? '0'
}

async function pollUntil<T>(
  fetcher: () => Promise<T>,
  done: (value: T) => boolean,
  timeoutMs: number,
): Promise<T> {
  const deadline = Date.now() + timeoutMs
  let last = await fetcher()
  while (!done(last) && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 2000))
    last = await fetcher()
  }
  return last
}
