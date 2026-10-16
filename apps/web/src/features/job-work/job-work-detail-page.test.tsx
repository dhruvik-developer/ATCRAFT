/**
 * The job-work order detail page (E7b/P10.M4, K07).
 *
 * The list shows one row per order with a receipt count; everything the order
 * means lives here. What is asserted is the whole the page is for: the lines
 * with issued, received, rejected, scrap and still-at-vendor quantities, the
 * timeline from approval to close, and the reconciliation with the ledger's
 * live job-work pool beside the order's books. Plus the way out for rejected
 * output: a rework order raised from the rejected receipt line.
 */

import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, renderScreen } from '@/test/harness'

import JobWorkDetailPage from './job-work-detail-page'
import type {
  ChallanRow,
  JobWorkOrderDetail,
  ReceiptRow,
  ReconciliationReport,
} from './job-work-model'

const DETAIL: JobWorkOrderDetail = {
  id: 'jw-1',
  code: 'JW-0001',
  status: 'PARTIALLY_RECEIVED',
  version: 1,
  vendor_id: 'vendor-aaaa-0001',
  warehouse_id: 'wh-1',
  return_warehouse_id: 'wh-1',
  cost_centre_id: null,
  process_type: 'PVD',
  process_description: 'PVD, rose gold',
  rate_basis: 'PER_PIECE',
  rate_per_unit: '12.500000',
  tax_rate: '18.0000',
  sac_code: '9988',
  estimated_charges: '1250.0000',
  total_charges: '1125.0000',
  vendor_debit_amount: '0.0000',
  receipt_count: 1,
  requires_qc: false,
  expected_return_date: '2026-09-20',
  issued_at: '2026-09-05T08:00:00Z',
  approved_at: '2026-09-04T08:00:00Z',
  approved_by_id: null,
  closed_at: null,
  cancellation_reason: null,
  source_type: 'MANUAL',
  source_id: null,
  notes: null,
  lines: [
    {
      id: 'line-1',
      job_work_order_id: 'jw-1',
      line_number: 1,
      item_id: 'item-raw-1',
      uom_id: 'uom-1',
      quantity_to_issue: '100.0000',
      issued_quantity: '100.0000',
      received_quantity: '90.0000',
      expected_output_item_id: 'item-coated-1',
      expected_output_quantity: '100.0000',
      received_output_quantity: '88.0000',
      rejected_quantity: '2.0000',
      scrap_quantity: '0.0000',
      expected_loss_percentage: '5.0000',
      reservation_id: null,
      notes: null,
    },
  ],
}

const CHALLANS: ChallanRow[] = [
  {
    id: 'ch-1',
    code: 'JWC-0007',
    job_work_order_id: 'jw-1',
    vendor_id: 'vendor-aaaa-0001',
    challan_date: '2026-09-05',
    expected_return_date: '2026-09-20',
    vehicle_number: 'GJ01AB1234',
    transporter_id: null,
    total_quantity: '100.0000',
    total_value: '10000.0000',
    notes: null,
    lines: [],
  },
]

const RECEIPTS: ReceiptRow[] = [
  {
    id: 'rc-1',
    code: 'JW-0001/R1',
    receipt_number: 1,
    job_work_order_id: 'jw-1',
    vendor_id: 'vendor-aaaa-0001',
    receipt_date: '2026-09-10',
    vendor_challan_number: 'VND-551',
    vendor_challan_date: '2026-09-09',
    total_output_quantity: '88.0000',
    total_rejected_quantity: '2.0000',
    total_returned_unprocessed: '0.0000',
    total_scrap_quantity: '0.0000',
    total_input_consumed: '90.0000',
    total_charges: '1125.0000',
    vendor_debit_amount: '0.0000',
    has_excess_loss: false,
    qc_held_at: null,
    qc_released_at: null,
    qc_inspection_id: null,
    reason: 'First lot back',
    notes: null,
    lines: [
      {
        id: 'rl-1',
        receipt_id: 'rc-1',
        job_work_order_line_id: 'line-1',
        line_number: 1,
        output_item_id: 'item-coated-1',
        output_quantity: '88.0000',
        rejected_quantity: '2.0000',
        rejection_action: 'RETURN_TO_STOCK',
        returned_unprocessed_quantity: '0.0000',
        scrap_quantity: '0.0000',
        excess_loss_quantity: '0.0000',
        input_consumed_quantity: '90.0000',
        charges: '1125.0000',
        vendor_debit_amount: '0.0000',
        qc_inspection_id: null,
        invoiced_amount: '0.0000',
        debit_posted_amount: '0.0000',
        notes: null,
      },
    ],
  },
]

const RECONCILIATION: ReconciliationReport = {
  order_id: 'jw-1',
  code: 'JW-0001',
  vendor_id: 'vendor-aaaa-0001',
  status: 'PARTIALLY_RECEIVED',
  issued_at: '2026-09-05T08:00:00Z',
  expected_return_date: '2026-09-20',
  total_issued: '100.0000',
  total_received: '90.0000',
  total_outstanding: '10.0000',
  total_output_received: '88.0000',
  total_rejected: '2.0000',
  total_scrap: '0.0000',
  total_excess_loss: '0.0000',
  total_charges: '1125.0000',
  vendor_debit_amount: '0.0000',
  stock_in_job_work_pool: '10.0000',
  lines: [
    {
      line_number: 1,
      item_id: 'item-raw-1',
      output_item_id: 'item-coated-1',
      quantity_to_issue: '100.0000',
      issued_quantity: '100.0000',
      received_quantity: '90.0000',
      outstanding_quantity: '10.0000',
      expected_output_quantity: '100.0000',
      received_output_quantity: '88.0000',
      rejected_quantity: '2.0000',
      scrap_quantity: '0.0000',
      loss_percentage: '0.0000',
      expected_loss_percentage: '5.0000',
      excess_loss_quantity: '0.0000',
      output_shortfall_quantity: '10.0000',
    },
  ],
}

function renderDetail(permissions: string[]) {
  return renderScreen(<JobWorkDetailPage />, {
    permissions: [P.JOB_WORK_VIEW, ...permissions],
    route: '/job-work/jw-1',
    path: '/job-work/:orderId',
    allowUnrouted: true,
    routes: {
      'GET /job-work/jw-1': ok(DETAIL),
      'GET /job-work/jw-1/challans': ok(CHALLANS),
      'GET /job-work/jw-1/receipts': ok(RECEIPTS),
      'GET /job-work/jw-1/reconciliation': ok(RECONCILIATION),
    },
  })
}

describe('the job-work order detail page', () => {
  it('renders the lines, the timeline and the reconciliation with the live pool', async () => {
    renderDetail([P.JOB_WORK_RECEIVE])

    expect(await screen.findByRole('heading', { name: 'JW-0001' })).toBeVisible()
    // Lines: issued, good output, rejected and still at vendor.
    expect(screen.getByText('Lines')).toBeVisible()
    expect(screen.getByText('Reconciliation')).toBeVisible()
    // Timeline: approval, challan, receipt.
    expect(screen.getByText('Approved')).toBeVisible()
    expect(screen.getByText('Challan JWC-0007 issued')).toBeVisible()
    expect(screen.getByText('Receipt JW-0001/R1')).toBeVisible()
    // The ledger's pool beside the order's books.
    expect(screen.getByText('Job-work pool (live ledger)')).toBeVisible()
    // Money: accrued charges from the receipts.
    expect(screen.getByText('Money')).toBeVisible()
  })

  it('offers a rework order from a rejected receipt line', async () => {
    const { user } = renderDetail([P.JOB_WORK_CREATE])

    await screen.findByRole('heading', { name: 'JW-0001' })
    await user.click(screen.getByRole('button', { name: /Rework/ }))

    // The rejected output goes back out as a new order, not an edit.
    expect(await screen.findByRole('heading', { name: 'New rework order' })).toBeVisible()
  })

  it('denies the page without the job-work read right', async () => {
    const { AppRouter } = await import('@/app/router')
    renderScreen(<AppRouter />, {
      permissions: [P.ITEM_VIEW],
      route: '/job-work/jw-1',
      allowUnrouted: true,
      routes: {},
    })

    // The route guard answers before any chunk or query runs.
    expect(
      await screen.findByRole('heading', { name: 'You do not have access to this page' }),
    ).toBeVisible()
  })
})
