/**
 * The receive dialog request body (E7b/P10.M6, P10.M8).
 *
 * The backend has carried `is_final`, `approve_excess_loss` and
 * `rejection_action` for a while, but the dialog never sent them: a short
 * return could not be closed from the screen, excess-loss approval was
 * API-only, and every rejection arrived as RETURN_TO_STOCK. What is asserted
 * here is the exact body the dialog posts, plus the two gates around it - the
 * excess approval is offered only to `job_work.close` holders, and the vendor
 * challan date rides along with the vendor challan number.
 */

import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, renderScreen } from '@/test/harness'

import { JobWorkReceiveDialog } from './job-work-dialogs'
import type { JobWorkOrderDetail, JobWorkOrderLine } from './job-work-model'

const LINE: JobWorkOrderLine = {
  id: 'line-1',
  job_work_order_id: 'jw-1',
  line_number: 1,
  item_id: 'item-raw-1',
  uom_id: 'uom-1',
  quantity_to_issue: '100.0000',
  issued_quantity: '100.0000',
  received_quantity: '0.0000',
  expected_output_item_id: 'item-coated-1',
  expected_output_quantity: '100.0000',
  received_output_quantity: '0.0000',
  rejected_quantity: '0.0000',
  scrap_quantity: '0.0000',
  expected_loss_percentage: '5.0000',
  reservation_id: null,
  notes: null,
}

const DETAIL: JobWorkOrderDetail = {
  id: 'jw-1',
  code: 'JW-0001',
  status: 'ISSUED',
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
  total_charges: '0.0000',
  vendor_debit_amount: '0.0000',
  receipt_count: 0,
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
  lines: [LINE],
}

function renderReceive(permissions: string[]) {
  return renderScreen(
    <JobWorkReceiveDialog open onOpenChange={() => {}} orderId="jw-1" orderCode="JW-0001" />,
    {
      permissions: [P.JOB_WORK_VIEW, P.JOB_WORK_RECEIVE, ...permissions],
      routes: {
        'GET /job-work/jw-1': ok(DETAIL),
        'POST /job-work/jw-1/receive': ok({ received: true }),
      },
    },
  )
}

describe('the receive dialog request body', () => {
  it('sends the close-line flag, the rejection action with its reason, and the vendor challan date', async () => {
    const { user, fetch } = renderReceive([P.JOB_WORK_CLOSE])

    // A short lot: 90 good, 2 rejected, nothing unworked.
    await user.clear(await screen.findByLabelText('Received'))
    await user.type(screen.getByLabelText('Received'), '90')
    await user.type(screen.getByLabelText('Rejected'), '2')

    // The rejection controls appear once something is rejected.
    expect(await screen.findByLabelText('Rejection action')).toBeVisible()
    await user.type(screen.getByLabelText('Rejection reason'), '  Plating peeled  ')

    fireEvent.change(screen.getByLabelText('Vendor challan date'), {
      target: { value: '2026-09-09' },
    })

    await user.click(screen.getByLabelText('Close line - write off shortfall'))
    await user.type(screen.getByRole('textbox', { name: 'Reason' }), '  Short lot closed  ')
    await user.click(screen.getByRole('button', { name: 'Record receipt' }))

    await waitFor(() => fetch.requestFor('POST', '/job-work/jw-1/receive'))
    const body = fetch.requestFor('POST', '/job-work/jw-1/receive') as {
      vendor_challan_date?: string | null
      approve_excess_loss?: boolean
      lines?: unknown
    }
    expect(body?.vendor_challan_date).toBe('2026-09-09')
    expect(body?.approve_excess_loss).toBe(false)
    expect(body?.lines).toEqual([
      {
        line_number: 1,
        output_quantity: '90',
        rejected_quantity: '2',
        rejection_action: 'RETURN_TO_STOCK',
        returned_unprocessed_quantity: '0',
        is_final: true,
        notes: 'Plating peeled',
      },
    ])
  })

  it('sends the excess-loss approval only from a job_work.close holder', async () => {
    const closer = renderReceive([P.JOB_WORK_CLOSE])
    expect(await screen.findByLabelText('Approve excess loss (debit vendor)')).toBeVisible()
    closer.unmount()

    renderReceive([])
    await screen.findByLabelText('Received')
    expect(screen.queryByLabelText('Approve excess loss (debit vendor)')).toBeNull()
  })

  it('shows the computed write-off against the allowance when a line is closed', async () => {
    const { user } = renderReceive([P.JOB_WORK_CLOSE])

    await user.clear(await screen.findByLabelText('Received'))
    await user.type(screen.getByLabelText('Received'), '90')
    await user.click(screen.getByLabelText('Close line - write off shortfall'))

    // 100 out, 90 back good: 10 becomes process loss, 5 of it within the 5%
    // allowance and 5 beyond it.
    expect(await screen.findByText(/10 still out becomes/)).toBeVisible()
    expect(screen.getByText(/5 beyond it/)).toBeVisible()
  })
})
