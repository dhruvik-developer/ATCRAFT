/**
 * The cost centre a job-work order's charges are booked to.
 *
 * An amendment always states the centre, because `cost_centre_id` is one of the
 * draft fields an explicit null clears on `PATCH /job-work/{id}`. That makes
 * the account shown no picker the case worth pinning: the form must send back
 * the centre the order already carries, never the null that would erase it.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import type { JobWorkOrderDetail, JobWorkOrderRow } from './job-work-model'
import { JobWorkOrderFormDialog } from './job-work-order-form-dialog'

const ITEM = {
  id: 'it-handle',
  code: 'HDL-200',
  name: 'Pull handle 200mm',
  is_active: true,
  is_batch_tracked: false,
}
const VENDOR = { id: 'vn-shine', code: 'VEN-0012', name: 'Shine Platers', is_approved: true }
const WAREHOUSE = { id: 'wh-main', code: 'MAIN', name: 'Main store' }

const STAMP = '2026-09-01T09:00:00Z'
const CENTRES = [
  {
    id: 'cc-buff',
    code: 'CC-BUFF',
    name: 'Buffing and polishing',
    cost_centre_type: 'PRODUCTION',
    parent_id: null,
    plant_id: null,
    head_user_id: null,
    description: null,
    is_active: true,
    created_at: STAMP,
    updated_at: STAMP,
  },
  {
    id: 'cc-plate',
    code: 'CC-PLATE',
    name: 'Plating and PVD',
    cost_centre_type: 'PRODUCTION',
    parent_id: null,
    plant_id: null,
    head_user_id: null,
    description: null,
    is_active: true,
    created_at: STAMP,
    updated_at: STAMP,
  },
]

const ROW: JobWorkOrderRow = {
  id: 'jw-1',
  code: 'JW-0001',
  status: 'DRAFT',
  version: 1,
  vendor_id: VENDOR.id,
  warehouse_id: WAREHOUSE.id,
  return_warehouse_id: WAREHOUSE.id,
  cost_centre_id: 'cc-buff',
  process_type: 'POLISHING',
  process_description: 'Mirror polish, 400 grit',
  rate_basis: 'PER_PIECE',
  rate_per_unit: '12.5000',
  tax_rate: '18.0000',
  sac_code: '9988',
  estimated_charges: '1250.0000',
  total_charges: '0.0000',
  vendor_debit_amount: '0.0000',
  receipt_count: 0,
  requires_qc: false,
  expected_return_date: '2099-04-10',
  issued_at: null,
  approved_at: null,
  approved_by_id: null,
  closed_at: null,
  cancellation_reason: null,
  source_type: 'MANUAL',
  source_id: null,
  notes: null,
}

const DETAIL: JobWorkOrderDetail = {
  ...ROW,
  lines: [
    {
      id: 'jl-1',
      job_work_order_id: ROW.id,
      line_number: 1,
      item_id: ITEM.id,
      is_batch_tracked: false,
      batch_id: null,
      uom_id: 'uom-pcs',
      quantity_to_issue: '100.0000',
      issued_quantity: '0.0000',
      expected_output_item_id: ITEM.id,
      expected_output_quantity: '100.0000',
      expected_loss_percentage: '0.0000',
      received_quantity: '0.0000',
      received_output_quantity: '0.0000',
      rejected_quantity: '0.0000',
      scrap_quantity: '0.0000',
      reservation_id: null,
      notes: null,
    },
  ],
}

const routes = {
  'GET /job-work/jw-1': ok(DETAIL),
  // The pickers label the values the order already holds.
  'GET /vendors': ok(VENDOR),
  'GET /products/items': ok(ITEM),
  'GET /warehouses': page([WAREHOUSE]),
  'PATCH /job-work/jw-1': ok(DETAIL),
}

describe('job-work order form', () => {
  it('loads and offers a batch only after choosing a batch-tracked item', async () => {
    const trackedItem = { ...ITEM, is_batch_tracked: true }
    const { user } = renderScreen(<JobWorkOrderFormDialog open onOpenChange={() => {}} />, {
      permissions: [P.JOB_WORK_CREATE, P.WAREHOUSE_VIEW],
      routes: {
        ...routes,
        'GET /products/items': page([trackedItem]),
        'GET /inventory/batches': page([
          {
            id: 'batch-001',
            item_id: trackedItem.id,
            batch_number: 'LOT-001',
            available_quantity: '25.0000',
          },
        ]),
      },
    })

    await user.click(screen.getByRole('combobox', { name: 'Line 1 material' }))
    await user.type(screen.getByRole('textbox', { name: 'Search items' }), trackedItem.code)
    await user.click(await screen.findByRole('option', { name: /HDL-200/ }))
    await user.click(await screen.findByRole('combobox', { name: 'Batch' }))

    expect(await screen.findByRole('option', { name: /LOT-001/ })).toBeVisible()
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: /create draft order/i }))
    expect(await screen.findByText('Choose a batch for batch-tracked material')).toBeVisible()
  })

  it('sends the centre chosen, to an account that may read centres', async () => {
    const { user, fetch } = renderScreen(
      <JobWorkOrderFormDialog open onOpenChange={() => {}} order={ROW} />,
      {
        permissions: [P.JOB_WORK_CREATE, P.WAREHOUSE_VIEW, P.COST_CENTRE_VIEW],
        routes: { ...routes, 'GET /organization/cost-centres': page(CENTRES) },
      },
    )

    const dialog = await screen.findByRole('dialog')
    await user.click(await within(dialog).findByRole('combobox', { name: 'Cost centre' }))
    await user.click(await screen.findByRole('option', { name: 'CC-PLATE - Plating and PVD' }))
    await user.click(within(dialog).getByRole('button', { name: /save draft/i }))

    const body = (await waitFor(() => fetch.requestFor('PATCH', '/job-work/jw-1'))) as Record<
      string,
      unknown
    >
    expect(body.cost_centre_id).toBe('cc-plate')
  })

  it('sends back the centre the order carries from an account shown no picker', async () => {
    const { user, fetch } = renderScreen(
      <JobWorkOrderFormDialog open onOpenChange={() => {}} order={ROW} />,
      { permissions: [P.JOB_WORK_CREATE, P.WAREHOUSE_VIEW], routes },
    )

    const dialog = await screen.findByRole('dialog')
    const save = within(dialog).getByRole('button', { name: /save draft/i })
    // Enabled once the order's lines have landed and the form is seeded.
    await waitFor(() => expect(save).toBeEnabled())
    expect(within(dialog).queryByRole('combobox', { name: 'Cost centre' })).not.toBeInTheDocument()

    await user.click(save)

    const body = (await waitFor(() => fetch.requestFor('PATCH', '/job-work/jw-1'))) as Record<
      string,
      unknown
    >
    // Not null: null is the instruction that clears it.
    expect(body.cost_centre_id).toBe('cc-buff')
    expect(fetch.calls.some((call) => call.path.startsWith('/organization/cost-centres'))).toBe(
      false,
    )
  })
})
