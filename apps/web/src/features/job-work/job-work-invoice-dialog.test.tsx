/**
 * Recording a job-work invoice: vendor, then order, then receipt lines.
 *
 * The dialog prefills each line from the billable feed - the unbilled charges
 * at the agreed rate - and posts `invoice_type: JOB_WORK` with one
 * `job_work_receipt_line_id` per line, every figure a string. Saving creates
 * the bill and submits it in the same action, so what is asserted here is both
 * request bodies.
 */

import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import type { BillableLine, JobWorkOrderRow } from './job-work-model'
import { JobWorkInvoiceDialog } from './job-work-invoice-dialog'

const VENDOR = { id: 'vn-1', code: 'VEN-0007', name: 'Chromeline Electroplaters' }

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

const BILLABLE: BillableLine = {
  job_work_receipt_line_id: 'rl-1',
  job_work_receipt_id: 'r-1',
  receipt_code: 'R-0001',
  receipt_date: '2026-05-10',
  line_number: 1,
  output_item_id: 'it-1',
  output_quantity: '58.0000',
  charges: '1073.0000',
  invoiced_amount: '0.0000',
  unbilled_amount: '1073.0000',
  vendor_debit_amount: '0.0000',
  pending_vendor_debit: '0.0000',
  rate_per_unit: '18.500000',
  rate_basis: 'PER_PIECE',
  tax_rate: '18.0000',
  sac_code: '9988',
}

const CREATED = {
  id: 'vi-1',
  code: 'VINV/2026-27/00041',
  vendor_invoice_number: 'CHR/2026/0101',
  status: 'DRAFT',
}

const SUBMITTED = { ...CREATED, status: 'PENDING_APPROVAL' }

describe('recording a job-work invoice', () => {
  it('creates the typed bill from the billable lines and submits it', async () => {
    const { user, fetch } = renderScreen(<JobWorkInvoiceDialog open onOpenChange={() => {}} />, {
      permissions: [P.VENDOR_INVOICE_CREATE, P.VENDOR_VIEW, P.JOB_WORK_VIEW],
      routes: {
        'GET /vendors': page([VENDOR]),
        'GET /vendors/vn-1': ok(VENDOR),
        'GET /job-work': page([ORDER]),
        'GET /job-work/jw-1/billable-lines': ok([BILLABLE]),
        'POST /finance/vendor-invoices': ok(CREATED),
        'POST /finance/vendor-invoices/vi-1/submit': ok(SUBMITTED),
      },
    })

    await screen.findByRole('dialog')

    // Step one: the vendor, then the order of that vendor.
    await user.click(await screen.findByRole('combobox', { name: 'Vendor' }))
    await user.click(await screen.findByRole('option', { name: /VEN-0007/ }))
    await user.click(await screen.findByRole('combobox', { name: 'Job-work order' }))
    await user.click(await screen.findByRole('option', { name: /JW-0001/ }))

    // The billable line arrives prefilled from the unbilled charges.
    expect(await screen.findByText(/Unbilled/)).toBeVisible()
    expect(screen.getByDisplayValue('58.0000')).toBeVisible()

    await user.type(
      screen.getByRole('textbox', { name: /vendor invoice number/i }),
      'CHR/2026/0101',
    )
    await user.click(screen.getByRole('button', { name: /save and submit/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('POST', '/finance/vendor-invoices'),
    )) as Record<string, unknown>
    expect(body).toEqual({
      vendor_id: 'vn-1',
      vendor_invoice_number: 'CHR/2026/0101',
      invoice_date: expect.any(String),
      invoice_type: 'JOB_WORK',
      job_work_order_id: 'jw-1',
      round_off_amount: '0',
      lines: [
        {
          job_work_receipt_line_id: 'rl-1',
          description: 'Processing charges - receipt R-0001 line 1',
          hsn_code: '9988',
          quantity: '58.0000',
          unit_price: '18.500000',
          discount_amount: '0',
          tax_rate: '18.0000',
          is_interstate: false,
        },
      ],
    })

    await waitFor(() => fetch.requestFor('POST', '/finance/vendor-invoices/vi-1/submit'))
  }, 120_000)
})
