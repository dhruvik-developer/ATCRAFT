/**
 * The job-work "Invoices" tab: bills raised against job-work orders.
 *
 * The tab lists the vendor-invoice register narrowed to `invoice_type =
 * JOB_WORK` and resolves each row's order code from one bounded orders read.
 * Recording is gated on `vendor_invoice.create` - the right the API declares
 * on the create and the submit alike - and the tab itself is offered only to
 * an account that may see vendor invoices at all.
 */

import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'
import type { RouteRequest } from '@/test/harness'

import type { VendorInvoiceSummary } from '@/features/finance/finance-types'
import type { JobWorkOrderRow } from './job-work-model'
import JobWorkPage from './job-work-page'

const ORDER: JobWorkOrderRow = {
  id: 'jw-1',
  code: 'JW-0001',
  status: 'ISSUED',
  version: 1,
  vendor_id: 'vn-1',
  warehouse_id: 'wh-1',
  return_warehouse_id: 'wh-1',
  cost_centre_id: null,
  process_type: 'COATING',
  process_description: 'Antique brass plate',
  rate_basis: 'PER_PIECE',
  rate_per_unit: '18.500000',
  tax_rate: '18.0000',
  sac_code: '9988',
  estimated_charges: '1073.0000',
  total_charges: '1073.0000',
  vendor_debit_amount: '0.0000',
  receipt_count: 1,
  requires_qc: false,
  expected_return_date: '2026-05-20',
  issued_at: '2026-05-07T09:00:00Z',
  approved_at: '2026-05-06T09:00:00Z',
  approved_by_id: null,
  closed_at: null,
  cancellation_reason: null,
  source_type: 'MANUAL',
  source_id: null,
  notes: null,
}

const INVOICE: VendorInvoiceSummary = {
  id: 'vi-1',
  code: 'VINV/2026-27/00041',
  vendor_id: 'vn-1',
  vendor_invoice_number: 'CHR/2026/0101',
  invoice_date: '2026-05-12',
  due_date: '2026-06-11',
  status: 'PENDING_APPROVAL',
  match_status: 'MATCHED',
  currency: 'INR',
  subtotal: '1073.00',
  tax_amount: '193.14',
  total_amount: '1266.14',
  paid_amount: '0.00',
  debit_allocated_amount: '0.00',
  outstanding_amount: '1266.14',
  purchase_order_id: null,
  goods_receipt_id: null,
  invoice_type: 'JOB_WORK',
  job_work_order_id: 'jw-1',
  cost_centre_id: null,
  created_at: '2026-05-12T09:00:00Z',
}

function renderInvoices(permissions: string[], routes: Record<string, unknown>) {
  return renderScreen(<JobWorkPage />, {
    permissions: [P.JOB_WORK_VIEW, ...permissions],
    route: '/?tab=invoices',
    routes: {
      'GET /finance/vendor-invoices': page([INVOICE]),
      'GET /job-work': page([ORDER]),
      ...routes,
    },
  })
}

describe('the invoices tab', () => {
  it('lists job-work bills with their order code, narrowed by type', async () => {
    const seen: { url?: URL } = {}
    renderInvoices([P.VENDOR_INVOICE_VIEW], {
      'GET /finance/vendor-invoices': ({ url }: RouteRequest) => {
        seen.url = url
        return page([INVOICE])
      },
    })

    expect(await screen.findByText('CHR/2026/0101')).toBeVisible()
    expect(screen.getByText('JW-0001')).toBeVisible()
    expect(seen.url?.searchParams.get('invoice_type')).toBe('JOB_WORK')
  })

  it('is offered only to an account that may see vendor invoices', async () => {
    renderScreen(<JobWorkPage />, {
      permissions: [P.JOB_WORK_VIEW],
      routes: { 'GET /job-work': page([ORDER]) },
    })

    expect(await screen.findByText('JW-0001')).toBeVisible()
    expect(screen.queryByRole('tab', { name: /invoices/i })).not.toBeInTheDocument()
  })

  it('gates recording on vendor_invoice.create', async () => {
    const viewer = renderInvoices([P.VENDOR_INVOICE_VIEW], {})
    expect(await screen.findByText('CHR/2026/0101')).toBeVisible()
    expect(
      screen.queryByRole('button', { name: /record job-work invoice/i }),
    ).not.toBeInTheDocument()
    viewer.unmount()

    renderInvoices([P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_CREATE], {})
    expect(await screen.findByText('CHR/2026/0101')).toBeVisible()
    expect(screen.getByRole('button', { name: /record job-work invoice/i })).toBeInTheDocument()
  })

  it('shows the shared match dialog from a row', async () => {
    const { user } = renderInvoices([P.VENDOR_INVOICE_VIEW, P.VENDOR_INVOICE_APPROVE], {
      'GET /finance/vendor-invoices/vi-1': ok({ ...INVOICE, lines: [] }),
      'GET /finance/vendor-invoices/vi-1/match': ok({
        invoice_id: 'vi-1',
        invoice_code: 'VINV/2026-27/00041',
        vendor_id: 'vn-1',
        purchase_order_id: null,
        goods_receipt_id: null,
        job_work_order_id: 'jw-1',
        matched: true,
        match_status: 'MATCHED',
        price_tolerance: '0.02',
        quantity_tolerance: '0',
        total_invoiced: '1073.00',
        total_expected: '1073.00',
        total_variance: '0.00',
        worst_variance: '0.00',
        blocking_reasons: [],
        has_overbilling: false,
        lines: [],
      }),
    })

    await screen.findByText('CHR/2026/0101')
    await user.click(screen.getByRole('button', { name: 'CHR/2026/0101' }))
    expect(await screen.findByRole('dialog')).toBeVisible()
  })
})
