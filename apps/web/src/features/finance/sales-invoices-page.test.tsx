import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'
import { RaiseSalesInvoiceDialog, SalesInvoiceDetailDialog } from './sales-invoice-dialogs'
import SalesInvoicesPage from './sales-invoices-page'
import { SalesReceiptAllocationDialog } from './sales-receipt-allocation-dialog'
import type { SalesInvoiceDetail, SalesInvoicePreview } from './sales-invoice-types'
import type { PaymentDetail } from './payment-model'

const ID = '01a00000-0000-7000-8000-000000000021'
const DISPATCH = '01a00000-0000-7000-8000-000000000022'
const preview: SalesInvoicePreview = {
  dispatch_id: DISPATCH,
  sales_order_id: 'order',
  customer_id: 'customer',
  invoice_date: '2026-10-12',
  due_date: '2026-11-11',
  currency: 'INR',
  is_interstate: false,
  place_of_supply_state_code: '27',
  seller_name: 'Atcraft',
  seller_gstin: null,
  seller_address: 'Pune',
  seller_state_code: '27',
  seller_state_name: 'Maharashtra',
  buyer_name: 'Hardware Dealer',
  buyer_gstin: null,
  buyer_state_code: '27',
  buyer_state_name: 'Maharashtra',
  billing_address: 'Pune',
  shipping_address: 'Pune warehouse',
  transporter_name: null,
  vehicle_number: null,
  lr_number: null,
  eway_bill_number: null,
  bank_name: null,
  bank_account_name: null,
  bank_account_number: null,
  bank_ifsc: null,
  bank_branch: null,
  invoice_terms: null,
  notes: null,
  subtotal: '100',
  discount_amount: '0',
  taxable_amount: '100',
  cgst_amount: '9',
  sgst_amount: '9',
  igst_amount: '0',
  tax_amount: '18',
  round_off_amount: '0',
  total_amount: '118',
  lines: [
    {
      dispatch_line_id: 'dispatch-line',
      sales_order_line_id: 'order-line',
      item_id: 'item',
      uom_id: 'uom',
      uom_code: 'NOS',
      line_number: 1,
      description: 'Architectural hinge',
      hsn_code: '8302',
      quantity: '10',
      unit_price: '10',
      tax_rate: '18',
      subtotal: '100',
      discount_amount: '0',
      taxable_amount: '100',
      cgst_amount: '9',
      sgst_amount: '9',
      igst_amount: '0',
      tax_amount: '18',
      line_total: '118',
    },
  ],
}
function invoice(overrides: Partial<SalesInvoiceDetail> = {}): SalesInvoiceDetail {
  return {
    ...preview,
    id: ID,
    code: 'SI/2026-27/00042',
    paid_amount: '0',
    balance_amount: '118',
    overdue_days: 0,
    status: 'ISSUED',
    issued_at: '2026-10-12T06:00:00Z',
    issued_by_id: 'user',
    pdf_document_id: null,
    can_cancel: true,
    cancelled_at: null,
    cancelled_by_id: null,
    cancellation_reason: null,
    journal_id: 'journal',
    payments: [],
    ...overrides,
  }
}

describe('sales invoices', () => {
  it('lists an issued invoice and opens its snapshotted detail', async () => {
    const { user } = renderScreen(<SalesInvoicesPage />, {
      permissions: [P.SALES_INVOICE_VIEW],
      route: '/finance/sales-invoices',
      routes: {
        'GET /finance/sales-invoices': page([invoice()]),
        [`GET /finance/sales-invoices/${ID}`]: ok(invoice()),
        'GET /customers': page([]),
      },
    })
    await user.click(await screen.findByRole('button', { name: 'SI/2026-27/00042' }))
    const dialog = await screen.findByRole('dialog', { name: 'SI/2026-27/00042' })
    expect(within(dialog).getByText('Architectural hinge')).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Cancel invoice' })).not.toBeInTheDocument()
  })
  it('does not fetch invoices without their view right', async () => {
    const { fetch } = renderScreen(<SalesInvoicesPage />, {
      routes: { 'GET /customers': page([]) },
    })
    await screen.findByRole('heading', { name: 'Sales invoices' })
    expect(fetch.calls.some((call) => call.path === '/finance/sales-invoices')).toBe(false)
  })
  it('issues with the reviewed dispatch id and optional date', async () => {
    const { user, fetch } = renderScreen(
      <RaiseSalesInvoiceDialog dispatchId={DISPATCH} onClose={() => {}} />,
      {
        permissions: [P.SALES_INVOICE_CREATE, P.SALES_INVOICE_VIEW],
        routes: {
          'GET /finance/sales-invoices/preview': ok(preview),
          'POST /finance/sales-invoices': ok(invoice()),
        },
      },
    )
    await screen.findByText('Architectural hinge')
    await user.type(screen.getByLabelText('Notes'), 'Invoice for shipment')
    await user.click(screen.getByRole('button', { name: 'Confirm and issue' }))
    await expect
      .poll(() => fetch.requestFor('POST', '/finance/sales-invoices'))
      .toEqual({ dispatch_id: DISPATCH, notes: 'Invoice for shipment' })
  })
  it('hides cancellation after the window or after payment', async () => {
    renderScreen(<SalesInvoiceDetailDialog invoiceId={ID} onClose={() => {}} />, {
      permissions: [P.SALES_INVOICE_VIEW, P.SALES_INVOICE_CANCEL],
      routes: {
        [`GET /finance/sales-invoices/${ID}`]: ok(
          invoice({
            can_cancel: false,
            status: 'PARTIALLY_PAID',
            paid_amount: '50',
            balance_amount: '68',
          }),
        ),
      },
    })
    await screen.findByText('Architectural hinge')
    expect(screen.queryByRole('button', { name: 'Cancel invoice' })).not.toBeInTheDocument()
  })
  it('sends an audited cancellation reason when allowed', async () => {
    const { user, fetch } = renderScreen(
      <SalesInvoiceDetailDialog invoiceId={ID} onClose={() => {}} />,
      {
        permissions: [P.SALES_INVOICE_VIEW, P.SALES_INVOICE_CANCEL],
        routes: {
          [`GET /finance/sales-invoices/${ID}`]: ok(invoice()),
          [`POST /finance/sales-invoices/${ID}/cancel`]: ok(
            invoice({ status: 'CANCELLED', can_cancel: false }),
          ),
        },
      },
    )
    await user.click(await screen.findByRole('button', { name: 'Cancel invoice' }))
    const confirm = await screen.findByRole('dialog', { name: 'Cancel this invoice?' })
    await user.type(within(confirm).getByLabelText('Reason'), 'Correct the buyer')
    await user.click(within(confirm).getByRole('button', { name: 'Cancel invoice' }))
    await expect
      .poll(() => fetch.requestFor('POST', `/finance/sales-invoices/${ID}/cancel`))
      .toEqual({ reason: 'Correct the buyer' })
  })
  it('allocates customer receipts with sales_invoice_id', async () => {
    const payment: PaymentDetail = {
      id: 'payment',
      code: 'PAY/2026-27/00001',
      payment_type: 'RECEIPT',
      party_type: 'CUSTOMER',
      party_id: 'customer',
      payment_date: '2026-10-12',
      amount: '118',
      allocated_amount: '0',
      unallocated_amount: '118',
      currency: 'INR',
      payment_method: 'CASH',
      reference_number: null,
      status: 'APPROVED',
      created_at: '2026-10-12T06:00:00Z',
      bank_name: null,
      instrument_date: null,
      approved_by_id: 'user',
      approved_at: '2026-10-12T06:00:00Z',
      cancellation_reason: null,
      notes: null,
      version: 1,
      allocations: [],
    }
    const { user, fetch } = renderScreen(
      <SalesReceiptAllocationDialog payment={payment} onClose={() => {}} />,
      {
        permissions: [P.PAYMENT_RECORD, P.SALES_INVOICE_VIEW],
        routes: {
          'GET /finance/sales-invoices': page([invoice()]),
          'POST /finance/payments/payment/allocate': ok(payment),
        },
      },
    )
    await user.click(await screen.findByRole('button', { name: 'Add invoice' }))
    await user.click(screen.getByRole('button', { name: 'Save allocation' }))
    await expect
      .poll(() => fetch.requestFor('POST', '/finance/payments/payment/allocate'))
      .toEqual({
        replace: true,
        allocations: [{ sales_invoice_id: ID, amount: '118.00', notes: null }],
      })
  })
})
