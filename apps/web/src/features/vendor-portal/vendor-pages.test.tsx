import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { api } from '@/lib/api-client'
import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type RouteTable } from '@/test/harness'

import { VendorInvoiceForm } from './vendor-invoice-form'
import {
  canShipOrder,
  orderTab,
  receiptInvoiceLines,
  type VendorOrder,
  type VendorReceipt,
  type VendorShipment,
} from './vendor-model'
import {
  VendorHomePage,
  VendorInvoicePage,
  VendorInvoicesPage,
  VendorOrderPage,
  VendorOrdersPage,
  VendorPaymentsPage,
  VendorReceiptsPage,
  VendorShipmentsPage,
} from './vendor-pages'

const ID = '01a00000-0000-7000-8000-000000000101'
const VENDOR = '01a00000-0000-7000-8000-000000000102'
const LINE = '01a00000-0000-7000-8000-000000000103'
const RECEIPT = '01a00000-0000-7000-8000-000000000104'
const BASE = `/vendor-portal/purchase-orders/${ID}`
const rights = [
  P.VENDOR_PORTAL_ACCESS,
  P.VENDOR_PORTAL_RESPOND,
  P.VENDOR_PORTAL_SHIP,
  P.VENDOR_PORTAL_INVOICE,
]
const pagination = {
  page: 1,
  limit: 50,
  total: 0,
  total_pages: 0,
  has_next: false,
  has_previous: false,
}

function order(status: VendorOrder['status'] = 'SENT'): VendorOrder {
  return {
    id: ID,
    pending_amendment: false,
    amendment_number: 0,
    code: 'PO/2026-27/0007',
    po_date: '2026-09-01',
    status,
    tracking: {
      id: ID,
      code: 'PO/2026-27/0007',
      po_date: '2026-09-01',
      status,
      vendor_id: VENDOR,
      vendor_name: 'Brass Works',
      warehouse_id: VENDOR,
      currency: 'INR',
      total_amount: '212.40',
      tracking_status: 'NOT_DISPATCHED',
      created_at: '2026-09-01T00:00:00Z',
      item_count: 1,
      shipment_count: 0,
      days_late: 0,
      received_percentage: '0',
      grn_pending: false,
      qc_pending: false,
      documents_count: 0,
      has_lr_copy: false,
    },
    vendor_id: VENDOR,
    warehouse_id: VENDOR,
    currency: 'INR',
    subtotal: '180',
    discount: '20',
    tax_amount: '32.40',
    freight_amount: '0',
    total_amount: '212.40',
    payment_terms_days: 30,
    expected_delivery_date: '2026-09-15',
    vendor_committed_date: null,
    delivery_address: 'Atcraft, receiving warehouse',
    terms: 'Deliver during working hours',
    vendor_reference: null,
    sent_at: '2026-09-01T00:00:00Z',
    acknowledged_at: null,
    ready_for_dispatch_at: null,
    ready_packages: null,
    ready_note: null,
    vendor_decline_reason: null,
    assigned_vendor_user_id: null,
    version: 1,
    lines: [
      {
        id: LINE,
        line_number: 1,
        item_id: ID,
        item_code: 'BRASS-01',
        item_name: 'Brass body',
        item_category_id: null,
        description: null,
        quantity: '2',
        uom_id: VENDOR,
        uom_code: 'NOS',
        unit_price: '100',
        discount_percentage: '10',
        discount: '20',
        tax_rate: '18',
        tax_amount: '32.4',
        line_total: '212.4',
        required_date: null,
        received_quantity: '0',
        accepted_quantity: '0',
        rejected_quantity: '0',
        returned_quantity: '0',
        requires_qc: true,
        status: 'PENDING',
      },
    ],
    timeline: [],
    invoice_summary: { count: 0, total: '0', paid: '0', balance: '0' },
    receipt_summary: { count: 0, qc_pending: false },
    shipment_count: 0,
    documents_count: 0,
    shipments: [],
    documents: [],
    receipts: [],
    shipments_pagination: pagination,
    documents_pagination: pagination,
    receipts_pagination: pagination,
  }
}
function receipt(): VendorReceipt {
  return {
    id: RECEIPT,
    code: 'GRN/2026-27/0008',
    purchase_order_id: ID,
    purchase_order_code: 'PO/2026-27/0007',
    receipt_date: '2026-09-16',
    status: 'POSTED',
    lines: [
      {
        id: RECEIPT,
        line_number: 1,
        item_id: ID,
        purchase_order_line_id: LINE,
        received_quantity: '2',
        accepted_quantity: '1.5',
        rejected_quantity: '0.5',
        rejection_reason: 'Surface scratches',
        qc_status: 'COMPLETED',
      },
    ],
    inspections: [],
  }
}
function shipment(): VendorShipment {
  return {
    id: RECEIPT,
    purchase_order_id: ID,
    shipment_number: 2,
    transporter_id: null,
    courier_name: 'Safe Logistics',
    tracking_reference: 'TRACK-7',
    lr_number: 'LR-7',
    vehicle_number: null,
    driver_name: null,
    driver_mobile: null,
    shipped_at: '2026-09-10T04:00:00Z',
    delivered_at: null,
    expected_delivery_date: '2026-09-15',
    packages: 3,
    weight_kg: '12.5',
    freight_amount: '50',
    notes: null,
    created_at: '2026-09-10T04:00:00Z',
  }
}
function detail(
  status: VendorOrder['status'] = 'SENT',
  permissions = rights,
  extra: RouteTable = {},
) {
  const current = order(status)
  return renderScreen(<VendorOrderPage />, {
    route: `/vendor/orders/${ID}`,
    path: '/vendor/orders/:orderId',
    permissions,
    user: { vendor_id: VENDOR },
    routes: {
      [`GET ${BASE}`]: ok(current),
      [`GET ${BASE}/shipments`]: page([]),
      [`GET ${BASE}/documents`]: page([]),
      [`GET ${BASE}/receipts`]: page([]),
      ...extra,
    },
  })
}

describe('vendor order boundary and navigation', () => {
  it('links each dashboard tile to its matching server filter and keeps currencies out of counts', async () => {
    renderScreen(<VendorHomePage />, {
      permissions: rights,
      user: { vendor_id: VENDOR },
      routes: {
        'GET /vendor-portal/me': ok({
          id: VENDOR,
          name: 'Brass Works',
          categories: [{ id: ID, code: 'BODY', name: 'Lock bodies' }],
        }),
        'GET /vendor-portal/dashboard': ok({
          to_accept: 2,
          accepted: 3,
          ready: 4,
          in_transit: 5,
          delivered: 6,
          payment_pending: 7,
          qc_rejected: 8,
          qc_lookback_days: 90,
        }),
        'GET /vendor-portal/rating': ok({
          rating: '4.5',
          on_time_percentage: '96',
          rejection_percentage: '1.2',
          window_days: 90,
          receipt_count: 20,
          reason: 'Calculated from posted receipts',
        }),
      },
    })
    expect(await screen.findByText('Your categories: Lock bodies')).toBeVisible()
    for (const [label, href] of [
      ['To accept', '/vendor/orders?tab=to_accept'],
      ['Accepted', '/vendor/orders?tab=accepted'],
      ['Ready for dispatch', '/vendor/orders?tab=ready'],
      ['In transit', '/vendor/orders?tab=dispatched'],
      ['Delivered', '/vendor/orders?tab=delivered'],
      ['Payment pending', '/vendor/invoices?unpaid_only=1'],
      ['QC rejected', '/vendor/receipts?rejected_only=1'],
    ])
      expect(await screen.findByRole('link', { name: new RegExp(label!) })).toHaveAttribute(
        'href',
        href,
      )
    expect(await screen.findByText('90 days · 20 receipts')).toBeVisible()
  })
  it('sends tab, assignment, pagination and explicit search to the server', async () => {
    const requests: URL[] = []
    const view = renderScreen(<VendorOrdersPage />, {
      permissions: rights,
      user: { vendor_id: VENDOR },
      route: '/vendor/orders?tab=accepted&page=3',
      routes: {
        'GET /vendor-portal/purchase-orders/pending-amendments': page([]),
        'GET /vendor-portal/purchase-orders': ({ url }) => {
          requests.push(url)
          return page([order().tracking])
        },
      },
    })
    await screen.findByRole('link', { name: 'PO/2026-27/0007' })
    expect(requests.at(-1)?.searchParams.get('tab')).toBe('accepted')
    expect(requests.at(-1)?.searchParams.get('page')).toBe('3')
    await view.user.click(screen.getByRole('button', { name: 'To accept' }))
    await waitFor(() => expect(requests.at(-1)?.searchParams.get('tab')).toBe('to_accept'))
    expect(requests.at(-1)?.searchParams.get('page')).toBe('1')
    await view.user.click(screen.getByRole('checkbox', { name: 'Assigned to me' }))
    await waitFor(() => expect(requests.at(-1)?.searchParams.get('assigned_to_me')).toBe('true'))
    await view.user.type(screen.getByRole('textbox', { name: 'Search orders' }), 'BRASS')
    await view.user.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() => expect(requests.at(-1)?.searchParams.get('search')).toBe('BRASS'))
  })
  it.each([
    ['SENT', rights, true, false],
    ['ACKNOWLEDGED', rights, false, true],
    ['PARTIALLY_RECEIVED', rights, false, true],
    ['CANCELLED', rights, false, false],
    ['SENT', [P.VENDOR_PORTAL_ACCESS], false, false],
    ['ACKNOWLEDGED', [P.VENDOR_PORTAL_ACCESS, P.VENDOR_PORTAL_RESPOND], false, false],
  ] as const)(
    'offers state transitions only with the exact right (%s)',
    async (status, permissions, respond, ship) => {
      detail(status, [...permissions])
      await screen.findByRole('heading', { name: 'PO/2026-27/0007' })
      expect(Boolean(screen.queryByRole('button', { name: 'Accept' }))).toBe(respond)
      expect(Boolean(screen.queryByRole('button', { name: 'Decline' }))).toBe(respond)
      expect(Boolean(screen.queryByRole('button', { name: 'Add shipment' }))).toBe(ship)
      expect(Boolean(screen.queryByRole('button', { name: 'Mark ready for dispatch' }))).toBe(ship)
    },
  )
  it('shows a safe server error with retry instead of crashing a missing order', async () => {
    detail('SENT', rights, { [`GET ${BASE}`]: fail(404, 'NOT_FOUND', 'Purchase order not found') })
    expect(await screen.findByRole('alert')).toHaveTextContent('Purchase order not found')
    expect(screen.getByRole('button', { name: 'Try again' })).toBeVisible()
  })
  it('uses the embedded first pages and fetches older rows only when requested', async () => {
    const current = order()
    current.shipments = [shipment()]
    current.shipments_pagination = { ...pagination, total: 51, total_pages: 2, has_next: true }
    const view = detail('SENT', rights, {
      [`GET ${BASE}`]: ok(current),
      [`GET ${BASE}/shipments`]: page([{ ...shipment(), shipment_number: 1 }], {
        page: 2,
        total: 51,
        total_pages: 2,
        has_previous: true,
      }),
    })
    expect(await screen.findByRole('link', { name: 'Shipment 2' })).toBeVisible()
    expect(view.fetch.calls.filter((call) => call.path === `${BASE}/shipments`)).toHaveLength(0)
    await view.user.click(screen.getByRole('button', { name: 'More shipments' }))
    expect(await screen.findByRole('link', { name: 'Shipment 1' })).toBeVisible()
    expect(view.fetch.calls.filter((call) => call.path === `${BASE}/shipments`)).toHaveLength(1)
  })
})

describe('vendor order action payloads', () => {
  it('accepts with a separate committed date and vendor reference', async () => {
    const view = detail('SENT', rights, { [`POST ${BASE}/accept`]: ok({ id: ID }) })
    await view.user.click(await screen.findByRole('button', { name: 'Accept' }))
    const modal = within(screen.getByRole('dialog'))
    fireEvent.change(modal.getByLabelText('Committed delivery date'), {
      target: { value: '2026-09-20' },
    })
    await view.user.type(modal.getByLabelText('Your reference'), 'OUR-7')
    await view.user.click(modal.getByRole('button', { name: 'Accept purchase order' }))
    await waitFor(() =>
      expect(view.fetch.requestFor('POST', `${BASE}/accept`)).toEqual({
        committed_delivery_date: '2026-09-20',
        vendor_reference: 'OUR-7',
        note: null,
      }),
    )
  })
  it('validates a required decline reason and maps server field errors inline', async () => {
    const view = detail('SENT', rights, {
      [`POST ${BASE}/decline`]: fail(422, 'VALIDATION_ERROR', 'Validation failed', {
        fields: { reason: ['Give a more specific reason'] },
      }),
    })
    await view.user.click(await screen.findByRole('button', { name: 'Decline' }))
    const modal = within(screen.getByRole('dialog'))
    await view.user.click(modal.getByRole('button', { name: 'Decline purchase order' }))
    expect(await modal.findByText('Enter at least 3 characters')).toBeVisible()
    expect(
      view.fetch.calls.filter((call) => call.method === 'POST' && call.path.endsWith('/decline')),
    ).toHaveLength(0)
    await view.user.type(modal.getByLabelText('Reason'), 'Unable to supply')
    await view.user.click(modal.getByRole('button', { name: 'Decline purchase order' }))
    expect(await modal.findByText('Give a more specific reason')).toBeVisible()
    expect(view.fetch.requestFor('POST', `${BASE}/decline`)).toEqual({ reason: 'Unable to supply' })
  })
  it('changes the vendor commitment with a reason, without changing the planning date', async () => {
    const view = detail('ACKNOWLEDGED', rights, { [`POST ${BASE}/delivery-date`]: ok({ id: ID }) })
    await view.user.click(await screen.findByRole('button', { name: 'Change delivery date' }))
    const modal = within(screen.getByRole('dialog'))
    fireEvent.change(modal.getByLabelText('New committed delivery date'), {
      target: { value: '2026-09-25' },
    })
    await view.user.type(modal.getByLabelText('Reason'), 'Materials delayed')
    await view.user.click(modal.getByRole('button', { name: 'Change delivery date' }))
    await waitFor(() =>
      expect(view.fetch.requestFor('POST', `${BASE}/delivery-date`)).toEqual({
        date: '2026-09-25',
        reason: 'Materials delayed',
      }),
    )
  })
  it('marks ready with an integer package count and note', async () => {
    const view = detail('ACKNOWLEDGED', rights, {
      [`POST ${BASE}/ready-for-dispatch`]: ok({ id: ID }),
    })
    await view.user.click(await screen.findByRole('button', { name: 'Mark ready for dispatch' }))
    const modal = within(screen.getByRole('dialog'))
    await view.user.type(modal.getByLabelText('Packages'), '3')
    await view.user.type(modal.getByLabelText('Note'), 'Collect after noon')
    await view.user.click(modal.getByRole('button', { name: 'Mark ready for dispatch' }))
    await waitFor(() =>
      expect(view.fetch.requestFor('POST', `${BASE}/ready-for-dispatch`)).toEqual({
        packages: 3,
        note: 'Collect after noon',
      }),
    )
  })
  it('records the shipment with timezone-aware dispatch and decimal string money', async () => {
    const view = detail('ACKNOWLEDGED', rights, {
      'GET /vendor-portal/transporters/options': page([]),
      [`POST ${BASE}/shipments`]: ok(shipment()),
    })
    await view.user.click(await screen.findByRole('button', { name: 'Add shipment' }))
    const modal = within(screen.getByRole('dialog'))
    await view.user.type(modal.getByLabelText('Courier name'), 'Safe Logistics')
    await view.user.type(modal.getByLabelText('Tracking reference'), 'TRACK-7')
    await view.user.type(modal.getByLabelText('Packages'), '3')
    await view.user.type(modal.getByLabelText('Weight (kg)'), '12.5')
    await view.user.clear(modal.getByLabelText('Freight amount'))
    await view.user.type(modal.getByLabelText('Freight amount'), '50.01')
    fireEvent.change(modal.getByLabelText('Dispatch date and time'), {
      target: { value: '2026-09-10T09:30' },
    })
    fireEvent.change(modal.getByLabelText('Expected delivery date'), {
      target: { value: '2026-09-15' },
    })
    await view.user.click(modal.getByRole('button', { name: 'Record shipment' }))
    await waitFor(() =>
      expect(view.fetch.requestFor('POST', `${BASE}/shipments`)).toEqual({
        transporter_id: null,
        courier_name: 'Safe Logistics',
        tracking_reference: 'TRACK-7',
        lr_number: null,
        vehicle_number: null,
        driver_name: null,
        driver_mobile: null,
        shipped_at: new Date('2026-09-10T09:30').toISOString(),
        expected_delivery_date: '2026-09-15',
        packages: 3,
        weight_kg: '12.5',
        freight_amount: '50.01',
        notes: null,
      }),
    )
  })
  it('offers invoice-only uploads to the invoice role and posts multipart to the portal', async () => {
    const view = detail('SENT', [P.VENDOR_PORTAL_ACCESS, P.VENDOR_PORTAL_INVOICE], {
      [`POST ${BASE}/documents`]: ok({ id: RECEIPT, file_name: 'bill.pdf' }),
    })
    await view.user.click(await screen.findByRole('button', { name: 'Upload document' }))
    const modal = within(screen.getByRole('dialog'))
    expect(modal.getAllByRole('option').map((option) => option.textContent)).toEqual([
      'INVOICE',
      'Whole order',
    ])
    await view.user.upload(
      modal.getByLabelText('Image or PDF'),
      new File(['%PDF-1.7'], 'bill.pdf', { type: 'application/pdf' }),
    )
    await view.user.click(modal.getByRole('button', { name: 'Upload document' }))
    await waitFor(() =>
      expect(
        view.fetch.calls.some(
          (call) => call.method === 'POST' && call.path === `${BASE}/documents`,
        ),
      ).toBe(true),
    )
    const sent = vi
      .mocked(fetch)
      .mock.calls.find(
        ([path, options]) =>
          String(path).includes(`${BASE}/documents`) && options?.method === 'POST',
      )?.[1]?.body as FormData
    expect(sent.get('kind')).toBe('INVOICE')
    expect(sent.get('file')).toBeInstanceOf(File)
    expect(sent.has('shipment_id')).toBe(false)
  })
  it('uses the portal PDF job status and the shared download flow', async () => {
    const download = vi.spyOn(api, 'download').mockResolvedValue()
    const view = detail('SENT', rights, {
      [`POST ${BASE}/pdf`]: ok({
        job_id: RECEIPT,
        status: 'queued',
        status_url: `/api/v1/vendor-portal/jobs/${RECEIPT}`,
      }),
      [`GET /vendor-portal/jobs/${RECEIPT}`]: ok({
        id: RECEIPT,
        status: 'COMPLETED',
        row_count: 1,
        download_url: `/api/v1/vendor-portal/documents/${RECEIPT}/download`,
        document_id: RECEIPT,
      }),
    })
    await view.user.click(await screen.findByRole('button', { name: 'Download PO PDF' }))
    await view.user.click(await screen.findByRole('button', { name: 'Download' }))
    await waitFor(() =>
      expect(download).toHaveBeenCalledWith(`/vendor-portal/documents/${RECEIPT}/download`),
    )
    expect(view.fetch.calls.some((call) => call.path.startsWith('/reports/jobs/'))).toBe(false)
    download.mockRestore()
  })
})

describe('vendor bills, receipts and payments', () => {
  it('copies accepted quantities and the net order price without float arithmetic', () => {
    expect(receiptInvoiceLines(order(), receipt())).toEqual([
      {
        item_id: ID,
        purchase_order_line_id: LINE,
        goods_receipt_line_id: RECEIPT,
        description: 'Brass body',
        hsn_code: '',
        quantity: '1.5',
        uom_id: VENDOR,
        unit_price: '90',
        discount_amount: '0',
        tax_rate: '18',
        is_interstate: false,
      },
    ])
    expect(orderTab('invalid')).toBe('all')
    expect(canShipOrder(order('CLOSED'))).toBe(false)
  })
  it('submits an invoice against its receipt with exact decimal strings and no staff fields', async () => {
    const close = vi.fn()
    const view = renderScreen(<VendorInvoiceForm onClose={close} />, {
      permissions: rights,
      user: { vendor_id: VENDOR },
      routes: {
        'GET /vendor-portal/receipts': page([receipt()]),
        [`GET ${BASE}`]: ok(order('ACKNOWLEDGED')),
        'POST /vendor-portal/invoices': ok({ vendor_invoice_number: 'OUR/17' }),
      },
    })
    await screen.findByRole('option', { name: /PO\/2026-27\/0007.*GRN\/2026-27\/0008/ })
    await view.user.selectOptions(
      screen.getByLabelText('Purchase order and goods receipt'),
      RECEIPT,
    )
    await waitFor(() => expect(screen.getByLabelText('Quantity')).toHaveValue('1.5'))
    await view.user.type(screen.getByLabelText('Your invoice number'), 'OUR/17')
    fireEvent.change(screen.getByLabelText('Invoice date'), { target: { value: '2026-09-16' } })
    await view.user.click(screen.getByRole('button', { name: 'Submit invoice' }))
    await waitFor(() =>
      expect(view.fetch.requestFor('POST', '/vendor-portal/invoices')).toEqual({
        purchase_order_id: ID,
        goods_receipt_id: RECEIPT,
        vendor_invoice_number: 'OUR/17',
        invoice_date: '2026-09-16',
        currency: 'INR',
        round_off_amount: '0',
        notes: null,
        lines: [
          {
            item_id: ID,
            purchase_order_line_id: LINE,
            goods_receipt_line_id: RECEIPT,
            description: 'Brass body',
            hsn_code: null,
            quantity: '1.5',
            uom_id: VENDOR,
            unit_price: '90',
            discount_amount: '0',
            tax_rate: '18',
            is_interstate: false,
          },
        ],
      }),
    )
    expect(close).toHaveBeenCalledOnce()
  })
  it('does not upload the same attachment again when correcting a rejected invoice', async () => {
    let submitted = 0
    const view = renderScreen(<VendorInvoiceForm onClose={vi.fn()} />, {
      permissions: rights,
      user: { vendor_id: VENDOR },
      routes: {
        'GET /vendor-portal/receipts': page([receipt()]),
        [`GET ${BASE}`]: ok(order('ACKNOWLEDGED')),
        [`POST ${BASE}/documents`]: ok({ id: ID }),
        'POST /vendor-portal/invoices': () =>
          ++submitted === 1
            ? fail(422, 'VALIDATION_ERROR', 'Validation failed', {
                fields: { vendor_invoice_number: ['Invoice number already used'] },
              })
            : ok({ vendor_invoice_number: 'OUR/18' }),
      },
    })
    await screen.findByRole('option', { name: /PO\/2026-27\/0007.*GRN\/2026-27\/0008/ })
    await view.user.selectOptions(
      screen.getByLabelText('Purchase order and goods receipt'),
      RECEIPT,
    )
    await waitFor(() => expect(screen.getByLabelText('Quantity')).toHaveValue('1.5'))
    await view.user.type(screen.getByLabelText('Your invoice number'), 'OUR/17')
    fireEvent.change(screen.getByLabelText('Invoice date'), { target: { value: '2026-09-16' } })
    await view.user.upload(
      screen.getByLabelText('Invoice image or PDF (optional)'),
      new File(['%PDF-1.7'], 'bill.pdf', { type: 'application/pdf' }),
    )
    await view.user.click(screen.getByRole('button', { name: 'Submit invoice' }))
    expect(await screen.findByText('Invoice number already used')).toBeVisible()
    await view.user.clear(screen.getByLabelText('Your invoice number'))
    await view.user.type(screen.getByLabelText('Your invoice number'), 'OUR/18')
    await view.user.click(screen.getByRole('button', { name: 'Submit invoice' }))
    await waitFor(() => expect(submitted).toBe(2))
    expect(
      view.fetch.calls.filter(
        (call) => call.method === 'POST' && call.path === `${BASE}/documents`,
      ),
    ).toHaveLength(1)
  })
  it('shows QC reasons and requests rejected receipts on the server', async () => {
    let rejected: string | null = null
    renderScreen(<VendorReceiptsPage />, {
      permissions: rights,
      user: { vendor_id: VENDOR },
      route: '/vendor/receipts?rejected_only=1',
      routes: {
        'GET /vendor-portal/receipts': ({ url }) => {
          rejected = url.searchParams.get('rejected_only')
          return page([receipt()])
        },
      },
    })
    expect(await screen.findByText('Surface scratches')).toBeVisible()
    expect(rejected).toBe('true')
  })
  it('filters the top-level shipments and records delivered through its scoped order', async () => {
    const requests: URL[] = []
    const view = renderScreen(<VendorShipmentsPage />, {
      permissions: rights,
      user: { vendor_id: VENDOR },
      route: '/vendor/shipments?status=in_transit',
      routes: {
        'GET /vendor-portal/shipments': ({ url }) => {
          requests.push(url)
          return page([shipment()])
        },
        [`POST ${BASE}/shipments/${RECEIPT}/delivered`]: ok(shipment()),
      },
    })
    await view.user.click(await screen.findByRole('button', { name: 'Mark shipment 2 delivered' }))
    await waitFor(() =>
      expect(view.fetch.requestFor('POST', `${BASE}/shipments/${RECEIPT}/delivered`)).toEqual({}),
    )
    expect(requests[0]?.searchParams.get('status')).toBe('in_transit')
  })
  it('shows per-currency payment summaries and the allocation to the vendor invoice', async () => {
    renderScreen(<VendorPaymentsPage />, {
      permissions: rights,
      user: { vendor_id: VENDOR },
      routes: {
        'GET /vendor-portal/payments/summary': ok([
          {
            currency: 'USD',
            outstanding: '120',
            overdue: '10',
            due_next_seven_days: '40',
            paid_this_month: '30',
            last_payment_date: null,
            last_payment_amount: null,
          },
        ]),
        'GET /vendor-portal/payments': page([
          {
            id: ID,
            code: 'PAY/7',
            payment_date: '2026-09-17',
            amount: '30',
            unallocated_amount: '0',
            currency: 'USD',
            payment_method: 'BANK_TRANSFER',
            reference_number: 'UTR-7',
            allocations: [{ vendor_invoice_id: ID, vendor_invoice_number: 'OUR/17', amount: '30' }],
          },
        ]),
      },
    })
    expect(await screen.findByRole('heading', { name: 'USD payment summary' })).toBeVisible()
    expect(await screen.findByRole('link', { name: 'OUR/17' })).toHaveAttribute(
      'href',
      `/vendor/invoices/${ID}`,
    )
  })
  it('sends unpaid-only invoices without exposing staff edit or approval controls', async () => {
    let unpaid: string | null = null
    renderScreen(<VendorInvoicesPage />, {
      permissions: [P.VENDOR_PORTAL_ACCESS, P.VENDOR_PORTAL_INVOICE],
      user: { vendor_id: VENDOR },
      route: '/vendor/invoices?unpaid_only=1',
      routes: {
        'GET /vendor-portal/invoices': ({ url }) => {
          unpaid = url.searchParams.get('unpaid_only')
          return page([
            {
              id: ID,
              vendor_invoice_number: 'OUR/17',
              status: 'PENDING_APPROVAL',
              due_date: '2026-09-30',
              currency: 'INR',
              total_amount: '100',
              paid_amount: '0',
              outstanding_amount: '100',
            },
          ])
        },
      },
    })
    expect(await screen.findByRole('link', { name: 'OUR/17' })).toHaveAttribute(
      'href',
      `/vendor/invoices/${ID}`,
    )
    expect(unpaid).toBe('true')
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
  })
  it('renders the vendor-visible dispute reason on invoice detail', async () => {
    renderScreen(<VendorInvoicePage />, {
      permissions: rights,
      user: { vendor_id: VENDOR },
      route: `/vendor/invoices/${ID}`,
      path: '/vendor/invoices/:invoiceId',
      routes: {
        [`GET /vendor-portal/invoices/${ID}`]: ok({
          id: ID,
          code: 'VINV/7',
          vendor_invoice_number: 'OUR/17',
          status: 'DISPUTED',
          match_status: 'VARIANCE',
          invoice_date: '2026-09-01',
          due_date: '2026-09-30',
          currency: 'INR',
          total_amount: '100',
          paid_amount: '0',
          outstanding_amount: '100',
          dispute_reason: 'Quantity exceeds received goods',
          lines: [],
        }),
      },
    })
    expect(
      await screen.findByText('Buyer’s dispute: Quantity exceeds received goods'),
    ).toBeVisible()
  })
})

describe('vendor pending amendment projection', () => {
  const pending = {
    id: ID,
    code: 'PO/2026-27/0007',
    status: 'PENDING_APPROVAL',
    amendment_number: 2,
    last_amended_at: '2026-10-27T12:00:00Z',
    pending_amendment: true,
    message:
      'This order is being revised. Updated terms will be available after approval and resend.',
  }

  it('renders only revision status and offers no commercial actions until resend', async () => {
    const view = detail('SENT', rights, {
      [`GET ${BASE}`]: ok({
        ...pending,
        terms: 'UNAPPROVED CONFIDENTIAL TERMS',
        total_amount: '999999',
        lines: [{ item_name: 'PRIVATE NEW ITEM' }],
      }),
    })
    expect(await screen.findByText('Amended (rev 2)')).toBeVisible()
    expect(screen.getByText(pending.message)).toBeVisible()
    expect(screen.queryByText('UNAPPROVED CONFIDENTIAL TERMS')).toBeNull()
    expect(screen.queryByText('PRIVATE NEW ITEM')).toBeNull()
    expect(
      screen.queryByRole('button', { name: /Accept|Decline|shipment|invoice|PDF|document/i }),
    ).toBeNull()
    expect(
      view.fetch.calls.some(
        (call) => call.method === 'GET' && /\/(shipments|documents|receipts)$/.test(call.path),
      ),
    ).toBe(false)
    expect(screen.getByRole('button', { name: 'Refresh order' })).toBeVisible()
  })

  it('keeps pending amendments in their own paginated status-only list', async () => {
    const seen: { kind: string; page: string | null }[] = []
    renderScreen(<VendorOrdersPage />, {
      route: '/vendor/orders?page=3&amendments_page=2',
      permissions: rights,
      user: { vendor_id: VENDOR },
      routes: {
        'GET /vendor-portal/purchase-orders/pending-amendments': ({ url }) => {
          seen.push({ kind: 'amendments', page: url.searchParams.get('page') })
          return page([pending], { page: 2 })
        },
        'GET /vendor-portal/purchase-orders': ({ url }) => {
          seen.push({ kind: 'orders', page: url.searchParams.get('page') })
          return page([], { page: 3 })
        },
      },
    })
    expect(await screen.findByText('Amended (rev 2)')).toBeVisible()
    expect(screen.getByRole('link', { name: pending.code })).toHaveAttribute(
      'href',
      `/vendor/orders/${ID}`,
    )
    expect(seen).toContainEqual({ kind: 'amendments', page: '2' })
    expect(seen).toContainEqual({ kind: 'orders', page: '3' })
    expect(screen.queryByText('Total')).toBeNull()
  })

  it('shows the revision badge with commercial terms after the revised order is resent', async () => {
    detail('SENT', rights, { [`GET ${BASE}`]: ok({ ...order(), amendment_number: 2 }) })
    expect(await screen.findByText('Amended (rev 2)')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Accept' })).toBeVisible()
    expect(screen.getByText('Deliver during working hours')).toBeVisible()
  })
})
