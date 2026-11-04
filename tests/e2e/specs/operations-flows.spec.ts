/** API-driven coverage for the procurement and operations journeys in B5. */
import { createHash, randomUUID } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test } from '@playwright/test'

import { API_URL, BASE_URL } from '../lib/env.mjs'
import { accountForAudience } from '../lib/audiences.mjs'
import { errorToastFailures } from '../lib/navigation.mjs'
import { readyAdminToken, signInThroughForm } from '../lib/session.mjs'

test.use({ storageState: { cookies: [], origins: [] } })
test.describe.configure({ mode: 'serial' })

const RUN = randomUUID().replaceAll('-', '').slice(0, 8).toUpperCase()
const E2E_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const REPO_ROOT = resolve(E2E_DIR, '..', '..')

interface ApiError {
  message?: string
  details?: { conflicts?: unknown[] }
}

interface ApiEnvelope<T> {
  success: boolean
  data: T
  error?: ApiError
}

interface ApiReply<T> {
  status: number
  requestId: string | null
  body: ApiEnvelope<T>
}

interface Warehouse {
  id: string
}

interface Unit {
  id: string
  code: string
}

interface Vendor {
  id: string
  code: string
}

interface VendorCategory {
  item_category_id: string
  item_category_code: string | null
}

interface VendorUser {
  id: string
  email: string
  vendor_id: string | null
  is_active: boolean
}

interface VendorUserDetail extends VendorUser {
  allowed_category_ids: string[]
}

interface Role {
  id: string
  code: string
}

interface Item {
  id: string
  code: string
  base_uom_id: string
  category_id: string | null
}

interface PurchaseOrderLine {
  id: string
  item_id: string
  uom_id: string
  quantity: string
  unit_price: string
  requires_qc: boolean
}

interface PurchaseOrder {
  id: string
  code: string
  status: string
  vendor_id: string
  warehouse_id: string
  assigned_vendor_user_id: string | null
  item_category_id: string | null
  lines: PurchaseOrderLine[]
}

interface GoodsReceiptLine {
  id: string
  purchase_order_line_id: string
  item_id: string
  accepted_quantity: string
  uom_id: string
  qc_status: string
}

interface GoodsReceipt {
  id: string
  status: string
  lines: GoodsReceiptLine[]
}

interface CostCentre {
  id: string
  code: string
}

interface CostCentreExpense {
  cost_centre_id: string
  debit_amount: string
  credit_amount: string
}

interface Shift {
  id: string
  code: string
}

interface WorkCentre {
  id: string
}

interface StaffUser {
  id: string
  full_name: string
}

interface ProductionOrder {
  id: string
  status: string
}

interface ProductionWorkOrder {
  id: string
  status: string
}

interface ProductionOrderDetail extends ProductionOrder {
  work_orders: ProductionWorkOrder[]
}

interface ShiftProductionRow {
  shift: string | null
}

interface RequisitionLine {
  id: string
  item_id: string
}

interface Requisition {
  id: string
  lines: RequisitionLine[]
}

interface ConversionGroup {
  vendor_id: string
  item_category_id: string | null
  assigned_vendor_user_id: string | null
  purchase_order_id: string | null
}

interface ConversionResult {
  groups: ConversionGroup[]
  unresolved_lines: unknown[]
}

interface VendorProfile {
  id: string
  categories: Array<{ id: string; code: string }>
}

interface Principal {
  id: string
  vendor_id: string | null
}

interface VendorPortalDashboard {
  to_accept: number
}

interface InboundTrackingRow {
  id: string
  has_lr_copy: boolean
}

interface VendorPortalReceipt {
  id: string
  purchase_order_id: string
  lines: Array<{ qc_status: string }>
}

interface VendorInvoice {
  id: string
  status: string
  recovered_amount?: string
  outstanding_amount?: string
}

interface BillableLine {
  job_work_receipt_line_id: string
  output_item_id: string
  output_quantity: string
  rate_per_unit: string
  tax_rate: string | null
  pending_vendor_debit: string
}

interface AlertRule {
  key: string
  is_enabled: boolean
  threshold_minutes: number
  remind_every_minutes: number
}

interface Notification {
  notification_type: string
  title: string
  body: string
}

let token = ''
let vendor: Vendor
let warehouse: Warehouse
let unit: Unit

async function exchange<T>(
  bearer: string,
  method: 'GET' | 'POST' | 'PUT',
  path: string,
  data?: unknown,
): Promise<ApiReply<T>> {
  const response = await fetch(`${API_URL}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${bearer}`,
      'Content-Type': 'application/json',
    },
    ...(data === undefined ? {} : { body: JSON.stringify(data) }),
  })
  const requestId = response.headers.get('x-request-id')
  return {
    status: response.status,
    requestId,
    body: (await response.json()) as ApiEnvelope<T>,
  }
}

async function api<T>(
  bearer: string,
  method: 'GET' | 'POST' | 'PUT',
  path: string,
  data?: unknown,
): Promise<T> {
  const reply = await exchange<T>(bearer, method, path, data)
  const requestId = reply.requestId ? ` (request id ${reply.requestId})` : ''
  const errorDetails = JSON.stringify(reply.body.error ?? reply.body)
  expect(
    reply.status,
    `${method} ${path}: ${reply.status} ${reply.body.error?.message ?? ''}`,
  ).toBeGreaterThanOrEqual(200)
  expect(
    reply.status,
    `${method} ${path} returned ${reply.status}${requestId}: ${errorDetails}`,
  ).toBeLessThan(300)
  expect(reply.body.success, `${method} ${path} returned an unsuccessful envelope`).toBe(true)
  return reply.body.data
}

async function list<T>(bearer: string, path: string): Promise<T[]> {
  return api<T[]>(bearer, 'GET', path)
}

function suffix(): string {
  return randomUUID().replaceAll('-', '').slice(0, 8).toUpperCase()
}

function dateIn(days: number): string {
  const value = new Date()
  value.setUTCDate(value.getUTCDate() + days)
  return value.toISOString().slice(0, 10)
}

async function createItem(
  categoryId: string | null = null,
  canJobWork = false,
): Promise<Item> {
  const code = `E2E-B5-${suffix()}`
  return api<Item>(token, 'POST', '/products/items', {
    code,
    name: `B5 operations fixture ${code}`,
    item_group: 'COMPONENT',
    category_id: categoryId,
    base_uom_id: unit.id,
    production_type: 'BUY',
    can_purchase: true,
    can_sell: false,
    can_be_bom_component: false,
    can_job_work: canJobWork,
  })
}

async function createPurchaseOrder(
  item: Item,
  options: {
    vendorId?: string
    warehouseId?: string
    assignedVendorUserId?: string
    quantity?: string
    unitPrice?: string
  } = {},
): Promise<PurchaseOrder> {
  const order = await api<PurchaseOrder>(token, 'POST', '/procurement/purchase-orders', {
    vendor_id: options.vendorId ?? vendor.id,
    warehouse_id: options.warehouseId ?? warehouse.id,
    po_date: dateIn(0),
    expected_delivery_date: dateIn(14),
    priority: 5,
    currency: 'INR',
    freight_amount: '0',
    vendor_reference: `E2E-${suffix()}`,
    assigned_vendor_user_id: options.assignedVendorUserId ?? null,
    source_type: 'MANUAL',
    lines: [
      {
        line_number: 1,
        item_id: item.id,
        quantity: options.quantity ?? '2',
        uom_id: unit.id,
        unit_price: options.unitPrice ?? '10',
        requires_qc: false,
      },
    ],
  })
  for (const action of ['submit', 'approve', 'send'] as const) {
    await api(token, 'POST', `/procurement/purchase-orders/${order.id}/${action}`, {
      reason: `B5 E2E ${action}`,
      ...(action === 'send'
        ? { assigned_vendor_user_id: options.assignedVendorUserId ?? null }
        : {}),
    })
  }
  return api<PurchaseOrder>(token, 'GET', `/procurement/purchase-orders/${order.id}`)
}

async function postGoodsReceipt(order: PurchaseOrder, quantity: string): Promise<GoodsReceipt> {
  const draft = await api<GoodsReceipt>(token, 'POST', '/procurement/goods-receipts', {
    purchase_order_id: order.id,
    receipt_date: dateIn(0),
    vendor_delivery_note: `E2E-${suffix()}`,
    lines: [
      {
        line_number: 1,
        purchase_order_line_id: order.lines[0].id,
        received_quantity: quantity,
        accepted_quantity: quantity,
        rejected_quantity: '0',
      },
    ],
  })
  await api(token, 'POST', `/procurement/goods-receipts/${draft.id}/post`)
  return api<GoodsReceipt>(token, 'GET', `/procurement/goods-receipts/${draft.id}`)
}

async function createCostCentre(): Promise<CostCentre> {
  const code = `E2E-B5-${suffix()}`
  return api<CostCentre>(token, 'POST', '/organization/cost-centres', {
    code,
    name: `B5 E2E ${code}`,
    cost_centre_type: 'PRODUCTION',
  })
}

async function createAndApprovePurchaseInvoice(
  order: PurchaseOrder,
  receipt: GoodsReceipt,
  costCentreId: string,
): Promise<VendorInvoice> {
  const line = receipt.lines[0]
  const invoice = await api<VendorInvoice>(token, 'POST', '/finance/vendor-invoices', {
    vendor_id: order.vendor_id,
    vendor_invoice_number: `E2E-${suffix()}`,
    invoice_date: dateIn(0),
    due_date: dateIn(30),
    purchase_order_id: order.id,
    goods_receipt_id: receipt.id,
    cost_centre_id: costCentreId,
    lines: [
      {
        item_id: line.item_id,
        purchase_order_line_id: line.purchase_order_line_id,
        goods_receipt_line_id: line.id,
        cost_centre_id: costCentreId,
        quantity: line.accepted_quantity,
        uom_id: line.uom_id,
        unit_price: order.lines[0].unit_price,
        tax_rate: '0',
      },
    ],
  })
  await api(token, 'POST', `/finance/vendor-invoices/${invoice.id}/submit`, {
    reason: 'B5 E2E invoice is ready for approval',
  })
  return api<VendorInvoice>(
    token,
    'POST',
    `/finance/vendor-invoices/${invoice.id}/approve`,
    {},
  )
}

test.beforeAll(async () => {
  token = await readyAdminToken()
  ;[vendor] = await list<Vendor>(token, '/vendors?is_approved=true&is_active=true&page=1&limit=100')
  ;[warehouse] = await list<Warehouse>(
    token,
    '/warehouses?is_active=true&page=1&limit=100',
  )
  ;[unit] = await list<Unit>(token, '/products/uoms?is_active=true&page=1&limit=100')
  expect(vendor?.id, 'A seeded approved vendor is required').toBeTruthy()
  expect(warehouse?.id, 'A seeded active warehouse is required').toBeTruthy()
  expect(unit?.id, 'A seeded unit of measure is required').toBeTruthy()
})

test('cost centre invoice line is approved and appears on its statement', async () => {
  const costCentre = await createCostCentre()
  const item = await createItem()
  const order = await createPurchaseOrder(item)
  const receipt = await postGoodsReceipt(order, '2')
  const invoice = await createAndApprovePurchaseInvoice(order, receipt, costCentre.id)

  expect(invoice.status).toBe('APPROVED')
  const rows = await list<CostCentreExpense>(
    token,
    `/reports/cost-centre-expense?date_from=${dateIn(-1)}&date_to=${dateIn(1)}&cost_centre_id=${costCentre.id}&page=1&limit=100`,
  )
  expect(rows.some((row) => row.cost_centre_id === costCentre.id && Number(row.debit_amount) > 0))
    .toBe(true)
})

test('shift roster refuses double booking and shift production reports the booking', async () => {
  const [workCentre] = await list<WorkCentre>(
    token,
    '/routing/work-centers?is_active=true&page=1&limit=100',
  )
  expect(workCentre?.id, 'A seeded active work centre is required').toBeTruthy()

  const operator = await api<StaffUser>(token, 'POST', '/users', {
    email: `e2e.b5.${RUN}.${suffix()}.operator@atcraft.example`,
    password: `R0ster!${suffix()}Aa`,
    full_name: `B5 E2E operator ${suffix()}`,
    must_change_password: false,
  })
  const shift = await api<Shift>(token, 'POST', '/routing/shifts', {
    code: `E2E${suffix()}`,
    name: `B5 E2E shift ${suffix()}`,
    start_time: '06:00:00',
    end_time: '06:45:00',
    break_minutes: 0,
  })
  const assignment = {
    roster_date: dateIn(90),
    shift_id: shift.id,
    work_center_id: workCentre.id,
    operator_user_id: operator.id,
  }
  await api(token, 'POST', '/routing/roster/assignments', assignment)
  const conflict = await exchange(token, 'POST', '/routing/roster/assignments', assignment)
  expect(conflict.status).toBe(422)
  expect(
    JSON.stringify(conflict.body.error).toLowerCase(),
    'the overlap response must identify the clashing roster assignment',
  ).toContain(operator.full_name.toLowerCase())

  const openOrders = await list<ProductionOrder>(
    token,
    '/production?status=IN_PROCESS&page=1&limit=100',
  )
  expect(openOrders.length, 'Seed the operational WIP production orders').toBeGreaterThan(0)
  const firstOpenOrder = openOrders[0]
  if (!firstOpenOrder) throw new Error('Seed the operational WIP production orders')
  const order = await api<ProductionOrderDetail>(
    token,
    'GET',
    `/production/${firstOpenOrder.id}`,
  )
  const workOrder = order.work_orders.find((row) => row.status === 'IN_PROGRESS')
  expect(workOrder?.id, 'The seeded in-process order needs a running operation').toBeTruthy()
  await api(token, 'POST', `/production/${order.id}/entries`, {
    work_order_id: workOrder?.id,
    entry_date: dateIn(0),
    shift_id: shift.id,
    operator_user_id: operator.id,
    produced_quantity: '1',
  })

  const report = await list<ShiftProductionRow>(
    token,
    `/reports/shift-production?date_from=${dateIn(0)}&date_to=${dateIn(0)}&page=1&limit=100`,
  )
  expect(report.some((row) => row.shift === shift.code)).toBe(true)
})

test('one requisition converts into three category orders with scoped assignees', async () => {
  const vendors = await list<Vendor>(
    token,
    '/vendors?is_approved=true&is_active=true&page=1&limit=100',
  )
  const roles = await list<Role>(token, '/roles?page=1&limit=200')
  const vendorRole = roles.find((role) => role.code === 'VENDOR_OWNER')
  expect(vendorRole?.id, 'The seeded VENDOR_OWNER role is required').toBeTruthy()
  if (!vendorRole) throw new Error('The seeded VENDOR_OWNER role is required')

  const pairs: Array<{ vendor: Vendor; categoryId: string; email: string; userId?: string }> = []
  for (const candidate of vendors.filter((row) => row.code !== 'DM-VEN-PACK')) {
    const [categories, users] = await Promise.all([
      list<VendorCategory>(
        token,
        `/vendors/${candidate.id}/categories?page=1&limit=100`,
      ),
      list<VendorUser>(
        token,
        `/users?is_vendor_user=true&vendor_id=${candidate.id}&page=1&limit=100`,
      ),
    ])
    for (const category of categories) {
      if (pairs.some((pair) => pair.categoryId === category.item_category_id)) continue
      const pairKey = createHash('sha256')
        .update(`${candidate.id}:${category.item_category_id}`)
        .digest('hex')
        .slice(0, 32)
      const email = `e2e.b5.${RUN}.${pairKey}@atcraft.example`
      const existing = users.find((row) => row.email.toLowerCase() === email)
      let userId = existing?.id
      if (existing) {
        expect(existing.vendor_id).toBe(candidate.id)
        const detail = await api<VendorUserDetail>(token, 'GET', `/users/${existing.id}`)
        expect(detail.allowed_category_ids).toContain(category.item_category_id)
      } else {
        const created = await api<VendorUser>(token, 'POST', '/users', {
          email,
          password: `E2e!B5${suffix()}Vendor`,
          full_name: `B5 E2E ${category.item_category_code ?? 'category'} assignee`,
          vendor_id: candidate.id,
          role_ids: [vendorRole.id],
          allowed_category_ids: [category.item_category_id],
          must_change_password: false,
        })
        userId = created.id
      }
      pairs.push({
        vendor: candidate,
        categoryId: category.item_category_id,
        email,
        userId,
      })
      if (pairs.length === 3) break
    }
    if (pairs.length === 3) break
  }
  expect(pairs, 'Seed at least three qualified vendor/category pairs').toHaveLength(3)

  const items = await Promise.all(pairs.map((pair) => createItem(pair.categoryId)))
  const requisition = await api<Requisition>(token, 'POST', '/procurement/purchase-requisitions', {
    required_date: dateIn(14),
    warehouse_id: warehouse.id,
    purpose: `B5 category split ${RUN}`,
    lines: items.map((item, index) => ({
      line_number: index + 1,
      item_id: item.id,
      quantity: '2',
      uom_id: unit.id,
      required_date: dateIn(14),
      estimated_unit_price: '10',
      suggested_vendor_id: pairs[index].vendor.id,
    })),
  })
  await api(token, 'POST', `/procurement/purchase-requisitions/${requisition.id}/submit`, {
    reason: 'B5 E2E requisition review',
  })
  await api(token, 'POST', `/procurement/purchase-requisitions/${requisition.id}/approve`, {
    reason: 'B5 E2E requisition approved',
  })
  const conversion = await api<ConversionResult>(
    token,
    'POST',
    '/procurement/purchase-requisitions/convert-to-orders',
    {
      requisition_ids: [requisition.id],
      lines: requisition.lines.map((line, index) => ({
        requisition_line_id: line.id,
        vendor_id: pairs[index].vendor.id,
        unit_price: '10',
      })),
    },
  )

  expect(conversion.unresolved_lines).toHaveLength(0)
  expect(conversion.groups).toHaveLength(3)
  expect(new Set(conversion.groups.map((group) => group.item_category_id)).size).toBe(3)
  for (const group of conversion.groups) {
    const pair = pairs.find((candidate) => candidate.categoryId === group.item_category_id)
    expect(group.vendor_id).toBe(pair?.vendor.id)
    expect(group.assigned_vendor_user_id).toBe(pair?.userId)
  }

  const orderId = conversion.groups[0].purchase_order_id
  const assigneeId = conversion.groups[0].assigned_vendor_user_id
  expect(orderId).toBeTruthy()
  expect(assigneeId).toBeTruthy()
  await api(token, 'POST', `/procurement/purchase-orders/${orderId}/submit`, {
    reason: 'B5 E2E category order submitted',
  })
  await api(token, 'POST', `/procurement/purchase-orders/${orderId}/approve`, {
    reason: 'B5 E2E category order approved',
  })
  const sent = await api<PurchaseOrder>(
    token,
    'POST',
    `/procurement/purchase-orders/${orderId}/send`,
    { assigned_vendor_user_id: assigneeId, reason: 'B5 E2E category order sent' },
  )
  expect(sent.status).toBe('SENT')
})

test('vendor accepts, dispatches, shares LR, receives a GRN and submits its invoice', async ({
  request,
}) => {
  const vendorToken = await readyAdminToken(accountForAudience('vendor'))
  const [profile, principal] = await Promise.all([
    api<VendorProfile>(vendorToken, 'GET', '/vendor-portal/me'),
    api<Principal>(vendorToken, 'GET', '/auth/me'),
  ])
  expect(principal.vendor_id).toBe(profile.id)
  const categoryId = profile.categories[0]?.id
  expect(categoryId, 'The demo vendor must have an allowed category').toBeTruthy()
  if (!categoryId) throw new Error('The demo vendor must have an allowed category')

  const item = await createItem(categoryId)
  const order = await createPurchaseOrder(item, {
    vendorId: profile.id,
    assignedVendorUserId: principal.id,
    quantity: '2',
  })
  const dashboard = await api<VendorPortalDashboard>(
    vendorToken,
    'GET',
    '/vendor-portal/dashboard',
  )
  expect(dashboard.to_accept).toBeGreaterThan(0)
  const toAccept = await list<PurchaseOrder>(
    vendorToken,
    '/vendor-portal/purchase-orders?tab=to_accept&page=1&limit=100',
  )
  expect(toAccept.some((row) => row.id === order.id)).toBe(true)

  await api(vendorToken, 'POST', `/vendor-portal/purchase-orders/${order.id}/accept`, {
    committed_delivery_date: dateIn(7),
    vendor_reference: `V-${suffix()}`,
  })
  const shippedAt = new Date(Date.now() - 60_000)
  const shipment = await api<{ id: string }>(
    vendorToken,
    'POST',
    `/vendor-portal/purchase-orders/${order.id}/shipments`,
    {
      courier_name: 'B5 E2E Carrier',
      tracking_reference: `TRACK-${suffix()}`,
      lr_number: `LR-${suffix()}`,
      shipped_at: shippedAt.toISOString(),
      expected_delivery_date: dateIn(0),
      packages: 1,
      freight_amount: '0',
    },
  )

  const image = Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p5sAAAAASUVORK5CYII=',
    'base64',
  )
  const upload = await request.post(
    `${API_URL}/vendor-portal/purchase-orders/${order.id}/documents`,
    {
      headers: { Authorization: `Bearer ${vendorToken}` },
      multipart: {
        file: { name: 'e2e-lr.png', mimeType: 'image/png', buffer: image },
        kind: 'LR_COPY',
        shipment_id: shipment.id,
      },
    },
  )
  const uploadBody = (await upload.json()) as ApiEnvelope<{ id: string }>
  expect(
    upload.ok(),
    `vendor LR upload failed: ${upload.status()} ${uploadBody.error?.message ?? ''}`,
  ).toBe(true)
  expect(uploadBody.success).toBe(true)

  const inbound = await list<InboundTrackingRow>(
    token,
    `/procurement/inbound-tracking?search=${encodeURIComponent(order.code)}&page=1&limit=100`,
  )
  expect(inbound.some((row) => row.id === order.id && row.has_lr_copy)).toBe(true)

  const deliveredAt = new Date(shippedAt.getTime() + 1_000).toISOString()
  await api(
    vendorToken,
    'POST',
    `/vendor-portal/purchase-orders/${order.id}/shipments/${shipment.id}/delivered`,
    { delivered_at: deliveredAt },
  )
  const receipt = await postGoodsReceipt(order, '2')
  const vendorReceipts = await list<VendorPortalReceipt>(
    vendorToken,
    `/vendor-portal/purchase-orders/${order.id}/receipts?page=1&limit=100`,
  )
  const visibleReceipt = vendorReceipts.find((row) => row.id === receipt.id)
  expect(visibleReceipt?.lines[0]?.qc_status).toBeTruthy()

  const invoice = await api<VendorInvoice>(vendorToken, 'POST', '/vendor-portal/invoices', {
    purchase_order_id: order.id,
    goods_receipt_id: receipt.id,
    vendor_invoice_number: `V-E2E-${suffix()}`,
    invoice_date: dateIn(0),
    due_date: dateIn(30),
    lines: [
      {
        item_id: item.id,
        purchase_order_line_id: order.lines[0].id,
        goods_receipt_line_id: receipt.lines[0].id,
        quantity: receipt.lines[0].accepted_quantity,
        uom_id: unit.id,
        unit_price: order.lines[0].unit_price,
        tax_rate: '0',
      },
    ],
  })
  expect(invoice.status).toBe('PENDING_APPROVAL')
})

test('job work receipt bills its output and posts expense and loss recovery', async () => {
  const costCentre = await createCostCentre()
  const item = await createItem(null, true)
  const orderFromVendor = await createPurchaseOrder(item, {
    quantity: '2',
    unitPrice: '50',
  })
  await postGoodsReceipt(orderFromVendor, '2')

  const job = await api<{ id: string; vendor_debit_amount: string }>(
    token,
    'POST',
    '/job-work',
    {
      vendor_id: vendor.id,
      process_type: 'POLISHING',
      expected_return_date: dateIn(7),
      warehouse_id: warehouse.id,
      return_warehouse_id: warehouse.id,
      cost_centre_id: costCentre.id,
      rate_per_unit: '25',
      lines: [
        {
          line_number: 1,
          item_id: item.id,
          quantity_to_issue: '2',
          expected_output_item_id: item.id,
          expected_output_quantity: '2',
        },
      ],
    },
  )
  await api(token, 'POST', `/job-work/${job.id}/approve`, {
    reason: 'B5 E2E material reserved for processing',
  })
  await api(token, 'POST', `/job-work/${job.id}/issue`, {
    reason: 'B5 E2E material sent to processor',
  })
  const received = await api<{ order: { vendor_debit_amount: string } }>(
    token,
    'POST',
    `/job-work/${job.id}/receive`,
    {
      reason: 'B5 E2E return includes approved excess process loss',
      approve_excess_loss: true,
      lines: [{ line_number: 1, output_quantity: '1', is_final: true }],
    },
  )
  expect(Number(received.order.vendor_debit_amount)).toBeGreaterThan(0)

  const [billable] = await api<BillableLine[]>(
    token,
    'GET',
    `/job-work/${job.id}/billable-lines`,
  )
  expect(Number(billable?.pending_vendor_debit)).toBeGreaterThan(0)
  const invoice = await api<VendorInvoice>(token, 'POST', '/finance/vendor-invoices', {
    vendor_id: vendor.id,
    vendor_invoice_number: `JW-E2E-${suffix()}`,
    invoice_date: dateIn(0),
    due_date: dateIn(30),
    invoice_type: 'JOB_WORK',
    job_work_order_id: job.id,
    cost_centre_id: costCentre.id,
    lines: [
      {
        item_id: billable.output_item_id,
        job_work_receipt_line_id: billable.job_work_receipt_line_id,
        description: 'Polishing service',
        quantity: billable.output_quantity,
        uom_id: unit.id,
        unit_price: billable.rate_per_unit,
        tax_rate: billable.tax_rate ?? '0',
        cost_centre_id: costCentre.id,
      },
    ],
  })
  await api(token, 'POST', `/finance/vendor-invoices/${invoice.id}/submit`, {
    reason: 'B5 E2E job-work invoice submitted',
  })
  const approved = await api<VendorInvoice>(
    token,
    'POST',
    `/finance/vendor-invoices/${invoice.id}/approve`,
    {},
  )
  expect(approved.status).toBe('APPROVED')
  expect(Number(approved.recovered_amount)).toBeGreaterThan(0)

  const rows = await list<CostCentreExpense>(
    token,
    `/reports/cost-centre-expense?date_from=${dateIn(-1)}&date_to=${dateIn(1)}&cost_centre_id=${costCentre.id}&page=1&limit=100`,
  )
  expect(rows.some((row) => row.cost_centre_id === costCentre.id && Number(row.debit_amount) > 0))
    .toBe(true)
  expect(rows.some((row) => row.cost_centre_id === costCentre.id && Number(row.credit_amount) > 0))
    .toBe(true)
})

test('smart-alert sweep notifies both the PO creator and assigned vendor user', async () => {
  const vendorToken = await readyAdminToken(accountForAudience('vendor'))
  const [profile, principal] = await Promise.all([
    api<VendorProfile>(vendorToken, 'GET', '/vendor-portal/me'),
    api<Principal>(vendorToken, 'GET', '/auth/me'),
  ])
  const categoryId = profile.categories[0]?.id
  expect(categoryId, 'The demo vendor must have an allowed category').toBeTruthy()
  if (!categoryId) throw new Error('The demo vendor must have an allowed category')
  const item = await createItem(categoryId)
  const rules = await api<AlertRule[]>(token, 'GET', '/notifications/alert-rules')
  const original = rules.find((rule) => rule.key === 'PO_NOT_ACKNOWLEDGED')
  expect(original, 'The PO acknowledgement alert rule must be registered').toBeTruthy()

  try {
    await api(token, 'PUT', '/notifications/alert-rules', {
      rules: [
        {
          key: 'PO_NOT_ACKNOWLEDGED',
          is_enabled: true,
          threshold_minutes: 0,
          remind_every_minutes: original?.remind_every_minutes,
        },
      ],
    })
    const order = await createPurchaseOrder(item, {
      vendorId: profile.id,
      assignedVendorUserId: principal.id,
    })

    const python = [
      'import json',
      'from src.workers.tasks.notifications import scan_smart_alerts',
      'result = scan_smart_alerts.apply(throw=True)',
      'print(json.dumps(result.get()))',
    ].join('; ')
    const sweep = spawnSync(
      'docker',
      [
        'compose',
        '-f',
        resolve(REPO_ROOT, 'docker-compose.yml'),
        '-f',
        resolve(REPO_ROOT, 'docker-compose.prod.yml'),
        'exec',
        '-T',
        'backend',
        'python',
        '-c',
        python,
      ],
      { cwd: E2E_DIR, encoding: 'utf8', timeout: 60_000 },
    )
    expect(
      sweep.status,
      `The in-process smart-alert sweep failed: ${sweep.stderr || sweep.stdout}`,
    ).toBe(0)
    const outputLines = sweep.stdout.trim().split(/\r?\n/)
    const result = JSON.parse(outputLines[outputLines.length - 1]) as {
      enabled: boolean
      failed: number
    }
    expect(result.enabled).toBe(true)
    expect(result.failed).toBe(0)

    const [creatorFeed, vendorFeed] = await Promise.all([
      list<Notification>(
        token,
        '/notifications?notification_type=PURCHASE_ORDER_NOT_ACKNOWLEDGED&limit=100',
      ),
      list<Notification>(
        vendorToken,
        '/notifications?notification_type=PURCHASE_ORDER_NOT_ACKNOWLEDGED&limit=100',
      ),
    ])
    expect(creatorFeed.some((row) => row.title.includes(order.code))).toBe(true)
    expect(vendorFeed.some((row) => row.title.includes(order.code))).toBe(true)
  } finally {
    await api(token, 'PUT', '/notifications/alert-rules', {
      rules: [
        {
          key: 'PO_NOT_ACKNOWLEDGED',
          is_enabled: original?.is_enabled,
          threshold_minutes: original?.threshold_minutes,
          remind_every_minutes: original?.remind_every_minutes,
        },
      ],
    })
  }
})

test('purchase-order summary runs by category from the report screen', async ({ page }) => {
  token = token || (await readyAdminToken())
  await signInThroughForm(page)
  await page.goto(
    `${BASE_URL}/reports?report=PURCHASE_ORDER_SUMMARY&date_from=2026-09-01&date_to=2026-09-30`,
  )

  await expect(page.getByRole('heading', { name: 'Purchase order summary' })).toBeVisible()
  await page.getByRole('combobox', { name: 'Group by' }).click()
  await page.getByRole('option', { name: 'Category' }).click()

  const reportResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `${new URL(API_URL).pathname}/reports/purchase-order-summary` &&
      response.request().method() === 'GET',
  )
  await page.getByRole('button', { name: 'Run report' }).click()

  const response = await reportResponse
  expect(response.status()).toBe(200)
  expect(new URL(response.url()).searchParams.get('group_by')).toBe('category')
  expect(new URL(response.url()).searchParams.get('date_from')).toBe('2026-09-01')
  expect(new URL(response.url()).searchParams.get('date_to')).toBe('2026-09-30')
  await expect(errorToastFailures(page)).resolves.toEqual([])
})
