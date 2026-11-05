/** API-backed journeys for commercial features whose screens must agree with the ledger. */
import { randomUUID } from 'node:crypto'

import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

import { API_URL, BASE_URL } from '../lib/env.mjs'
import { createNavigator } from '../lib/navigation.mjs'
import { readyAdminToken, signInThroughForm } from '../lib/session.mjs'

test.use({ storageState: { cookies: [], origins: [] } })
test.describe.configure({ mode: 'serial' })

const RUN = randomUUID().replaceAll('-', '').slice(0, 8).toUpperCase()
let token = ''
let shared: Page
let navigate: (route: string) => Promise<void>

test.beforeAll(async ({ browser }) => {
  token = await readyAdminToken()
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

async function apiData<T>(
  request: APIRequestContext,
  method: 'get' | 'post',
  path: string,
  payload?: unknown,
): Promise<T> {
  const response = await request[method](`${API_URL}${path}`, {
    headers: {
      Authorization: ['Bearer', token].join(' '),
      ...(payload ? { 'Content-Type': 'application/json' } : {}),
    },
    ...(payload ? { data: payload } : {}),
  })
  const body = await response.json()
  expect(
    response.ok(),
    `${method.toUpperCase()} ${path} -> ${response.status()}: ${JSON.stringify(body)}`,
  ).toBeTruthy()
  return body.data as T
}

interface IdRecord {
  id: string
}

interface CustomerRecord extends IdRecord {
  code: string
  name: string
  outstanding_amount: string
}

interface WarehouseRecord extends IdRecord {
  code: string
}

interface OrderRecord extends IdRecord {
  status: string
  lines: Array<{ id: string }>
}

interface DispatchRecord extends IdRecord {
  code: string
  lines: Array<{ id: string }>
}

interface InvoiceLineRecord extends IdRecord {
  quantity: string
  line_total: string
}

interface InvoicePreview {
  dispatch_id: string
  total_amount: string
  tax_amount: string
}

interface InvoiceRecord extends IdRecord {
  code: string
  dispatch_id: string
  sales_order_id: string
  customer_id: string
  invoice_date: string
  due_date: string
  currency: string
  total_amount: string
  taxable_amount: string
  tax_amount: string
  paid_amount: string
  balance_amount: string
  status: string
  journal_id: string
  lines: InvoiceLineRecord[]
}

interface LedgerStatement {
  closing_balance: string
  rows: Array<{ journal_id: string; reference_code: string | null }>
}

interface InventoryBalance {
  item_id: string
  warehouse_id: string
  available_quantity: string
  qc_hold_quantity: string
}

interface SalesReturnRecord extends IdRecord {
  status: string
  lines: Array<{ qc_inspection_id: string | null; credited_quantity: string }>
}

interface CreditNoteRecord extends IdRecord {
  sales_return_id: string | null
  status: string
  taxable_amount: string
  tax_amount: string
  total_amount: string
}

interface ReportJobAccepted {
  job_id: string
  status: string
  status_url: string
}

interface ReportJobRecord extends IdRecord {
  status: string
  document_id: string | null
  error_message: string | null
}

interface WorklistRecord {
  customer_id: string
  overdue_amount: string
  follow_up_id: string | null
}

interface FollowUpRecord extends IdRecord {
  status: string
  promised_amount: string | null
  promised_date: string | null
}

interface CommercialScenario {
  customer: CustomerRecord
  warehouse: WarehouseRecord
  item: { id: string; code: string }
  order: OrderRecord
  dispatch: DispatchRecord
}

function dateOffset(days: number): string {
  const date = new Date()
  date.setUTCDate(date.getUTCDate() + days)
  return date.toISOString().slice(0, 10)
}

function minorUnits(value: string): bigint {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value)
  if (!match) throw new Error(`Invalid monetary amount: ${value}`)
  const sign = match[1] === '-' ? -1n : 1n
  const fraction = (match[3] ?? '').padEnd(2, '0').slice(0, 2)
  return sign * (BigInt(match[2]) * 100n + BigInt(fraction))
}

async function createDispatchedScenario(
  request: APIRequestContext,
  name: string,
  quantity: string,
  dispatchDate = dateOffset(0),
): Promise<CommercialScenario> {
  const [units, warehouses] = await Promise.all([
    apiData<Array<{ id: string; code: string }>>(request, 'get', '/products/uoms?limit=50'),
    apiData<WarehouseRecord[]>(
      request,
      'get',
      '/warehouses?limit=50&warehouse_type=FINISHED_GOODS',
    ),
  ])
  const unit = units.find((row) => row.code === 'NOS') ?? units[0]
  if (!unit) throw new Error('The seeded unit of measure is missing')
  let warehouse = warehouses[0]
  if (!warehouse) {
    warehouse = await apiData<WarehouseRecord>(request, 'post', '/warehouses', {
      code: `E2E-COMM-WH-${RUN}`,
      name: `E2E Commercial Warehouse ${RUN}`,
      warehouse_type: 'FINISHED_GOODS',
    })
  }

  const customer = await apiData<CustomerRecord>(request, 'post', '/customers', {
    code: `E2E-COMM-${name}-${RUN}`,
    name: `E2E Commercial ${name} ${RUN}`,
    customer_type: 'DEALER',
    credit_days: 30,
  })
  const item = await apiData<{ id: string; code: string }>(
    request,
    'post',
    '/products/items',
    {
      code: `E2E-COMM-ITEM-${name}-${RUN}`,
      name: `E2E Commercial Item ${name} ${RUN}`,
      item_group: 'COMPONENT',
      base_uom_id: unit.id,
      production_type: 'BUY',
      can_purchase: true,
      can_sell: true,
      standard_cost: '10.00',
      sale_price: '100.00',
      hsn_code: '8302',
      gst_rate: '18',
    },
  )

  const adjustment = await apiData<IdRecord>(request, 'post', '/inventory/adjustments', {
    warehouse_id: warehouse.id,
    reason_code: 'OPENING',
    reason: `Opening stock for commercial journey ${RUN}`,
    lines: [{ item_id: item.id, quantity, unit_cost: '10.00' }],
  })
  await apiData(request, 'post', `/inventory/adjustments/${adjustment.id}/post`)

  const order = await apiData<OrderRecord>(request, 'post', '/sales', {
    customer_id: customer.id,
    warehouse_id: warehouse.id,
    order_date: dispatchDate,
    required_date: dateOffset(14),
    lines: [{ line_number: 1, item_id: item.id, quantity, unit_price: '100.00' }],
  })
  await apiData(request, 'post', `/sales/${order.id}/confirm`, {
    reason: `Confirm commercial journey ${RUN}`,
  })

  const dispatch = await apiData<DispatchRecord>(request, 'post', '/dispatch', {
    dispatch_type: 'SALES',
    warehouse_id: warehouse.id,
    dispatch_date: dispatchDate,
    customer_id: customer.id,
    sales_order_id: order.id,
    lines: [
      {
        item_id: item.id,
        quantity,
        sales_order_line_id: order.lines[0].id,
        serial_ids: [],
        carton_ids: [],
      },
    ],
  })
  await apiData(request, 'post', `/dispatch/${dispatch.id}/ready`, {
    reason: `Ready commercial journey ${RUN}`,
  })
  await apiData(request, 'post', `/dispatch/${dispatch.id}/dispatch`, {
    reason: `Dispatch commercial journey ${RUN}`,
  })

  return { customer, warehouse, item, order, dispatch }
}

async function previewAndIssueInvoice(
  request: APIRequestContext,
  scenario: CommercialScenario,
  invoiceDate?: string,
): Promise<{ preview: InvoicePreview; invoice: InvoiceRecord }> {
  const query = new URLSearchParams({ dispatch_id: scenario.dispatch.id })
  if (invoiceDate) query.set('invoice_date', invoiceDate)
  const preview = await apiData<InvoicePreview>(
    request,
    'get',
    `/finance/sales-invoices/preview?${query.toString()}`,
  )
  const payload: { dispatch_id: string; invoice_date?: string } = {
    dispatch_id: scenario.dispatch.id,
  }
  if (invoiceDate) payload.invoice_date = invoiceDate
  const invoice = await apiData<InvoiceRecord>(
    request,
    'post',
    '/finance/sales-invoices',
    payload,
  )
  return { preview, invoice }
}

async function receiveAndAllocateInvoice(
  request: APIRequestContext,
  invoice: InvoiceRecord,
): Promise<void> {
  const currentInvoice = await apiData<InvoiceRecord>(
    request,
    'get',
    `/finance/sales-invoices/${invoice.id}`,
  )
  const amount = currentInvoice.balance_amount
  if (minorUnits(amount) <= 0n) {
    throw new Error(`Invoice ${invoice.code} has no outstanding balance to allocate`)
  }
  const payment = await apiData<IdRecord>(request, 'post', '/finance/payments', {
    payment_type: 'RECEIPT',
    party_type: 'CUSTOMER',
    party_id: invoice.customer_id,
    payment_date: dateOffset(0),
    amount,
    payment_method: 'CASH',
  })
  await apiData(request, 'post', `/finance/payments/${payment.id}/approve`, {
    reason: `Approve commercial receipt ${RUN}`,
  })
  await apiData(request, 'post', `/finance/payments/${payment.id}/allocate`, {
    allocations: [{ sales_invoice_id: invoice.id, amount }],
  })
}

async function getStockBalance(
  request: APIRequestContext,
  scenario: CommercialScenario,
): Promise<InventoryBalance> {
  const balances = await apiData<InventoryBalance[]>(
    request,
    'get',
    `/inventory/balances?item_id=${scenario.item.id}&warehouse_id=${scenario.warehouse.id}&limit=50`,
  )
  const balance = balances.find(
    (row) => row.item_id === scenario.item.id && row.warehouse_id === scenario.warehouse.id,
  )
  if (!balance) throw new Error(`No stock balance found for item ${scenario.item.code}`)
  return balance
}

async function waitForReportJob(
  request: APIRequestContext,
  jobId: string,
): Promise<ReportJobRecord> {
  const deadline = Date.now() + 120_000
  while (Date.now() < deadline) {
    const job = await apiData<ReportJobRecord>(request, 'get', `/reports/jobs/${jobId}`)
    if (job.status === 'COMPLETED') return job
    expect(job.status, `PDF job ${jobId} failed: ${job.error_message}`).not.toBe('FAILED')
    await shared.waitForTimeout(1_000)
  }
  throw new Error(`PDF job ${jobId} did not complete within 120 seconds`)
}

let paymentScenario: (CommercialScenario & { invoice: InvoiceRecord }) | undefined

test('a brand-tagged item is returned by the brand filter in the item list', async ({
  request,
}) => {
  interface IdRecord {
    id: string
  }
  interface Unit extends IdRecord {
    code: string
  }
  interface ItemRecord extends IdRecord {
    code: string
    brand_id: string
  }

  const code = `E2E-BRAND-${RUN}`
  const brand = await apiData<IdRecord>(request, 'post', '/products/brands', {
    code,
    name: `E2E Brand ${RUN}`,
  })
  const units = await apiData<Unit[]>(request, 'get', '/products/uoms?limit=50')
  const unit = units.find((row) => row.code === 'NOS') ?? units[0]
  expect(unit, 'the seeded unit of measure is missing').toBeTruthy()

  const item = await apiData<ItemRecord>(request, 'post', '/products/items', {
    code: `ITEM-${code}`,
    name: `E2E branded item ${RUN}`,
    item_group: 'COMPONENT',
    base_uom_id: unit.id,
    production_type: 'BUY',
    can_purchase: true,
    can_sell: true,
    brand_id: brand.id,
  })
  expect(item.brand_id).toBe(brand.id)

  const filtered = await apiData<ItemRecord[]>(
    request,
    'get',
    `/products/items?brand_id=${brand.id}`,
  )
  expect(
    filtered.some((row) => row.id === item.id && row.brand_id === brand.id),
    'the API brand filter did not return the tagged item',
  ).toBeTruthy()

  await navigate(`/items?brand_id=${brand.id}`)
  await shared.getByPlaceholder('Search code or name...').fill(item.code)
  const row = shared.getByRole('row').filter({ hasText: item.code })
  await expect(row).toBeVisible({ timeout: 15_000 })
  await expect(row).toContainText(`E2E Brand ${RUN}`)
})

test('a dispatch previews and issues an invoice with its AR journal and PDF', async ({
  request,
}) => {
  test.setTimeout(180_000)
  const scenario = await createDispatchedScenario(request, 'PAY', '1')
  const { preview, invoice } = await previewAndIssueInvoice(request, scenario)

  expect(preview.dispatch_id).toBe(scenario.dispatch.id)
  expect(minorUnits(preview.total_amount)).toBe(minorUnits(invoice.total_amount))
  expect(invoice.status).toBe('ISSUED')
  expect(invoice.journal_id).toBeTruthy()

  const statement = await apiData<LedgerStatement>(
    request,
    'get',
    `/finance/ledger/customer/${scenario.customer.id}?limit=50`,
  )
  expect(statement.rows.some((row) => row.journal_id === invoice.journal_id)).toBeTruthy()
  expect(minorUnits(statement.closing_balance)).toBe(minorUnits(invoice.total_amount))

  const customer = await apiData<CustomerRecord>(
    request,
    'get',
    `/customers/${scenario.customer.id}`,
  )
  expect(minorUnits(customer.outstanding_amount)).toBe(minorUnits(invoice.total_amount))
  const order = await apiData<OrderRecord>(request, 'get', `/sales/${scenario.order.id}`)
  expect(order.status).toBe('INVOICED')

  const accepted = await apiData<ReportJobAccepted>(
    request,
    'post',
    `/finance/sales-invoices/${invoice.id}/pdf`,
  )
  const job = await waitForReportJob(request, accepted.job_id)
  expect(job.document_id).toBeTruthy()
  paymentScenario = { ...scenario, invoice }
})

test('a customer receipt allocates to its invoice and closes the sales order', async ({
  request,
}) => {
  const scenario = paymentScenario
  if (!scenario) throw new Error('The invoice journey did not create its payment scenario')

  await receiveAndAllocateInvoice(request, scenario.invoice)
  const invoice = await apiData<InvoiceRecord>(
    request,
    'get',
    `/finance/sales-invoices/${scenario.invoice.id}`,
  )
  expect(invoice.status).toBe('PAID')
  expect(minorUnits(invoice.balance_amount)).toBe(0n)

  const beforeClose = await apiData<OrderRecord>(
    request,
    'get',
    `/sales/${scenario.order.id}`,
  )
  expect(beforeClose.status).toBe('INVOICED')
  const closed = await apiData<OrderRecord>(
    request,
    'post',
    `/sales/${scenario.order.id}/close`,
    { reason: `Close paid commercial order ${RUN}` },
  )
  expect(closed.status).toBe('CLOSED')
})

test('a returned dispatch is inspected, credited and netted from GST', async ({ request }) => {
  const invoiceDate = dateOffset(-14)
  const scenario = await createDispatchedScenario(request, 'RETURN', '2', invoiceDate)
  const { invoice } = await previewAndIssueInvoice(request, scenario, invoiceDate)
  const reportQuery = new URLSearchParams({
    date_from: invoice.invoice_date,
    date_to: dateOffset(0),
  })
  const gstBefore = await apiData<{ output: { taxable_amount: string; total_tax: string } }>(
    request,
    'get',
    `/finance/gst-summary?${reportQuery.toString()}`,
  )

  const returned = await apiData<SalesReturnRecord>(request, 'post', '/sales-returns', {
    dispatch_id: scenario.dispatch.id,
    lines: [
      {
        dispatch_line_id: scenario.dispatch.lines[0].id,
        quantity: '1',
        reason_code: 'CUSTOMER_RETURN',
        requires_qc: true,
      },
    ],
  })
  const received = await apiData<SalesReturnRecord>(
    request,
    'post',
    `/sales-returns/${returned.id}/receive`,
  )
  const inspectionId = received.lines[0].qc_inspection_id
  if (!inspectionId) throw new Error('Receiving the return did not create a QC inspection')
  const held = await getStockBalance(request, scenario)
  expect(minorUnits(held.qc_hold_quantity)).toBe(100n)
  expect(minorUnits(held.available_quantity)).toBe(0n)

  await apiData(request, 'post', `/quality/inspections/${inspectionId}/decide`, {
    accepted_quantity: '1',
    reason: `Accept returned stock ${RUN}`,
    close: true,
  })
  const closedReturn = await apiData<SalesReturnRecord>(
    request,
    'get',
    `/sales-returns/${returned.id}`,
  )
  expect(closedReturn.status).toBe('CLOSED')
  expect(minorUnits(closedReturn.lines[0].credited_quantity)).toBe(100n)
  const released = await getStockBalance(request, scenario)
  expect(minorUnits(released.qc_hold_quantity)).toBe(0n)
  expect(minorUnits(released.available_quantity)).toBe(100n)

  const creditNotes = await apiData<CreditNoteRecord[]>(
    request,
    'get',
    '/finance/credit-notes?limit=100',
  )
  const credit = creditNotes.find((note) => note.sales_return_id === returned.id)
  if (!credit) throw new Error(`Return ${returned.id} did not create its linked credit note`)
  expect(credit.status).toBe('ISSUED')
  expect(minorUnits(credit.total_amount)).toBe(minorUnits(invoice.total_amount) / 2n)

  const invoiceAfterCredit = await apiData<InvoiceRecord>(
    request,
    'get',
    `/finance/sales-invoices/${invoice.id}`,
  )
  expect(minorUnits(invoiceAfterCredit.balance_amount)).toBe(
    minorUnits(invoice.total_amount) - minorUnits(credit.total_amount),
  )
  await receiveAndAllocateInvoice(request, invoice)
  const paidInvoice = await apiData<InvoiceRecord>(
    request,
    'get',
    `/finance/sales-invoices/${invoice.id}`,
  )
  expect(paidInvoice.status).toBe('PAID')

  const gstAfter = await apiData<{ output: { taxable_amount: string; total_tax: string } }>(
    request,
    'get',
    `/finance/gst-summary?${reportQuery.toString()}`,
  )
  expect(
    minorUnits(gstBefore.output.taxable_amount) - minorUnits(gstAfter.output.taxable_amount),
  ).toBe(minorUnits(credit.taxable_amount))
  expect(minorUnits(gstBefore.output.total_tax) - minorUnits(gstAfter.output.total_tax)).toBe(
    minorUnits(credit.tax_amount),
  )
})

test('an overdue invoice appears on collections worklist and its follow-up can be promised and resolved', async ({
  request,
}) => {
  const invoiceDate = dateOffset(-90)
  const scenario = await createDispatchedScenario(request, 'COLL', '1', invoiceDate)
  const { invoice } = await previewAndIssueInvoice(request, scenario, invoiceDate)
  expect(invoice.due_date < dateOffset(0)).toBeTruthy()

  const worklistPath = `/collections/worklist?customer_id=${scenario.customer.id}&limit=50`
  const overdueRows = await apiData<WorklistRecord[]>(request, 'get', worklistPath)
  expect(
    overdueRows.some((row) => row.customer_id === scenario.customer.id),
    'the overdue invoice is missing from the collections worklist',
  ).toBeTruthy()

  const followUp = await apiData<FollowUpRecord>(request, 'post', '/collections/follow-ups', {
    customer_id: scenario.customer.id,
    currency: invoice.currency,
    next_action_date: dateOffset(1),
  })
  const assignedRows = await apiData<WorklistRecord[]>(request, 'get', worklistPath)
  const assignedRow = assignedRows.find((row) => row.customer_id === scenario.customer.id)
  expect(assignedRow?.follow_up_id).toBe(followUp.id)

  const promised = await apiData<FollowUpRecord>(
    request,
    'post',
    `/collections/follow-ups/${followUp.id}/promise`,
    { promised_amount: invoice.balance_amount, promised_date: dateOffset(5) },
  )
  expect(minorUnits(promised.promised_amount ?? '0')).toBe(minorUnits(invoice.balance_amount))
  expect(promised.promised_date).toBe(dateOffset(5))

  const resolved = await apiData<FollowUpRecord>(
    request,
    'post',
    `/collections/follow-ups/${followUp.id}/resolve`,
    { note: `Customer payment arrangement confirmed ${RUN}` },
  )
  expect(resolved.status).toBe('RESOLVED')
})
