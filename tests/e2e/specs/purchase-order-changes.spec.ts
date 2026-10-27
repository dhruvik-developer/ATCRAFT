/** Real revisions retain line identities, require approval and fit all supported widths. */
import { randomUUID } from 'node:crypto'

import { expect, test, type Page } from '@playwright/test'

import type { SchemaOf } from '../../../apps/web/src/types/api'
import { API_URL, BASE_URL } from '../lib/env.mjs'
import { errorToastFailures } from '../lib/navigation.mjs'
import { readyAdminToken, signInThroughForm } from '../lib/session.mjs'

test.use({ storageState: { cookies: [], origins: [] } })
test.describe.configure({ mode: 'serial' })

let token = ''
let vendor: SchemaOf<'VendorRead'>
let warehouse: SchemaOf<'WarehouseRead'>
let unit: SchemaOf<'UomOut'>

async function api<T>(method: 'GET' | 'POST', path: string, data?: unknown): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    ...(data === undefined ? {} : { body: JSON.stringify(data) }),
  })
  const body = await response.json()
  expect(response.ok, `${method} ${path}: ${response.status} ${body.error?.message ?? ''}`).toBe(
    true,
  )
  expect(body.success).toBe(true)
  return body.data as T
}

test.beforeAll(async () => {
  token = await readyAdminToken()
  ;[vendor] = await api<SchemaOf<'VendorRead'>[]>(
    'GET',
    '/vendors?is_approved=true&is_active=true&limit=1',
  )
  ;[warehouse] = await api<SchemaOf<'WarehouseRead'>[]>('GET', '/warehouses?is_active=true&limit=1')
  ;[unit] = await api<SchemaOf<'UomOut'>[]>('GET', '/products/uoms?is_active=true&limit=1')
  expect(vendor?.id, 'An approved vendor is required').toBeTruthy()
  expect(warehouse?.id, 'An active warehouse is required').toBeTruthy()
  expect(unit?.id, 'An active unit is required').toBeTruthy()
})

async function issuedOrder() {
  const suffix = randomUUID().replaceAll('-', '').slice(0, 10).toUpperCase()
  const item = await api<SchemaOf<'ItemDetail'>>('POST', '/products/items', {
    code: `E2E-D1-${suffix}`,
    name: `E2E revision ${suffix}`,
    item_group: 'SPARE',
    base_uom_id: unit.id,
    can_purchase: true,
    can_sell: false,
  } satisfies Partial<SchemaOf<'ItemCreate'>>)
  const draft: SchemaOf<'PurchaseOrderCreate'> = {
    vendor_id: vendor.id,
    warehouse_id: warehouse.id,
    priority: 5,
    currency: 'INR',
    freight_amount: '0',
    source_type: 'MANUAL',
    acknowledge_duplicates: false,
    lines: [
      {
        line_number: 1,
        item_id: item.id,
        quantity: '10',
        unit_price: '25.123456',
        requires_qc: false,
      },
    ],
  }
  const order = await api<SchemaOf<'PurchaseOrderDetail'>>(
    'POST',
    '/procurement/purchase-orders',
    draft,
  )
  for (const action of ['submit', 'approve', 'send']) {
    await api('POST', `/procurement/purchase-orders/${order.id}/${action}`, {
      reason: 'Browser revision fixture reviewed',
    })
  }
  return {
    order: await api<SchemaOf<'PurchaseOrderDetail'>>(
      'GET',
      `/procurement/purchase-orders/${order.id}`,
    ),
    draft,
  }
}

async function fits(page: Page) {
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth - innerWidth),
  ).toBeLessThanOrEqual(1)
  expect(await errorToastFailures(page)).toEqual([])
}

for (const width of [375, 768, 1440]) {
  test(`an issued order is revised with history at ${width}px`, async ({ page }, testInfo) => {
    const { order } = await issuedOrder()
    await page.setViewportSize({ width, height: 900 })
    await signInThroughForm(page)
    await page.goto(`${BASE_URL}/procurement/purchase-orders/${order.id}`)
    await page.getByRole('button', { name: 'Amend', exact: true }).click()
    const dialog = page.getByRole('dialog', {
      name: `Amend ${order.code}`,
      exact: true,
    })
    await dialog
      .getByRole('textbox', { name: 'Reason for amendment', exact: true })
      .fill('Reduce the order to the revised customer requirement')
    await dialog.getByRole('textbox', { name: 'Quantity for line 1', exact: true }).fill('7')
    await fits(page)
    const response = page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        new URL(response.url()).pathname.endsWith(`/purchase-orders/${order.id}/amend`),
    )
    await dialog.getByRole('button', { name: 'Save amendment', exact: true }).click()
    expect((await response).status()).toBe(200)
    await expect(dialog).toBeHidden()
    await expect(page.getByText('Rev 1', { exact: true }).first()).toBeVisible()
    await page.getByRole('button', { name: 'Show revision 1 changes', exact: true }).click()
    await expect(page.getByRole('region', { name: 'Before revision', exact: true })).toBeVisible()
    await expect(page.getByRole('region', { name: 'After revision', exact: true })).toBeVisible()
    const revised = await api<SchemaOf<'PurchaseOrderDetail'>>(
      'GET',
      `/procurement/purchase-orders/${order.id}`,
    )
    expect(revised.status).toBe('PENDING_APPROVAL')
    expect(revised.amendment_number).toBe(1)
    expect(revised.version).toBeGreaterThan(order.version)
    expect(revised.lines).toHaveLength(1)
    expect(order.lines).toHaveLength(1)
    expect(revised.lines?.[0].id).toBe(order.lines?.[0].id)
    expect(revised.lines?.[0].quantity).toBe('7.0000')
    expect(revised.lines?.[0].unit_price).toBe('25.123456')
    await fits(page)
    await page.screenshot({ path: testInfo.outputPath(`revision-${width}.png`), fullPage: true })
  })
}

test('a separate duplicate requires explicit approval confirmation in the UI', async ({ page }) => {
  const { draft } = await issuedOrder()
  const resolution = await api<SchemaOf<'PurchaseOrderDuplicateResolutionResult'>>(
    'POST',
    '/procurement/purchase-orders/duplicate-resolution',
    {
      action: 'CREATE_WITH_APPROVAL',
      reason: 'Separate site delivery must be reviewed',
      draft: { kind: 'MANUAL', purchase_order: draft },
    } satisfies SchemaOf<'PurchaseOrderDuplicateResolutionRequest'>,
  )
  const order = resolution.purchase_orders?.[0]
  if (!order) throw new Error('Duplicate resolution did not return the separate order')
  await api('POST', `/procurement/purchase-orders/${order.id}/submit`, {})
  await signInThroughForm(page)
  await page.goto(`${BASE_URL}/procurement/purchase-orders/${order.id}`)
  await page.getByRole('button', { name: 'Approve', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog
    .getByRole('textbox', { name: 'Reason', exact: true })
    .fill('Confirmed separate delivery requirement')
  await dialog.getByRole('button', { name: 'Approve order', exact: true }).click()
  await expect(
    dialog.getByText('Confirm that this duplicate demand is required before approving.', {
      exact: true,
    }),
  ).toBeVisible()
  await dialog
    .getByRole('checkbox', {
      name: 'I confirm that the duplicate demand is required',
      exact: true,
    })
    .check()
  await dialog.getByRole('button', { name: 'Approve order', exact: true }).click()
  await expect(dialog).toBeHidden()
  const approved = await api<SchemaOf<'PurchaseOrderDetail'>>(
    'GET',
    `/procurement/purchase-orders/${order.id}`,
  )
  expect(approved.status).toBe('APPROVED')
  expect(approved.duplicate_confirmation_reason).toBe('Confirmed separate delivery requirement')
  expect(approved.duplicate_confirmed_at).toBeTruthy()
  await fits(page)
})
