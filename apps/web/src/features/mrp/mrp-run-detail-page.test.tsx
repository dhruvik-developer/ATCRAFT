import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import type { MrpRequirementRow, MrpRunDetail, MrpSuggestionRow } from './mrp-types'
import MrpRunDetailPage from './mrp-run-detail-page'

const RUN_ID = '01a00000-0000-7000-8000-00000000mrp1'
const SUGGESTION_ID = '01a00000-0000-7000-8000-00000000sug1'
const TRANSFER_SUGGESTION_ID = '01a00000-0000-7000-8000-00000000sug2'
const REALLOCATION_SUGGESTION_ID = '01a00000-0000-7000-8000-00000000sug3'
const ITEM_ID = '01a00000-0000-7000-8000-0000000item1'
const VENDOR_ID = '01a00000-0000-7000-8000-00000000ven1'
const WAREHOUSE_ID = '01a00000-0000-7000-8000-00000000wh01'
const SOURCE_WAREHOUSE_ID = '01a00000-0000-7000-8000-00000000wh02'
const REQUIRED_DATE = '2026-11-10'

const run: MrpRunDetail = {
  id: RUN_ID,
  code: 'MRP-0001',
  status: 'COMPLETED',
  mode: 'SYNC',
  horizon_days: 30,
  warehouse_ids: [WAREHOUSE_ID],
  started_at: '2026-10-20T09:00:00Z',
  completed_at: '2026-10-20T09:00:01Z',
  demand_line_count: 1,
  suggestion_count: 1,
  created_at: '2026-10-20T09:00:00Z',
  celery_task_id: null,
  error_message: null,
  parameters: null,
  result_summary: null,
  warnings: [],
  suggestion_counts_by_status: { OPEN: 1 },
  status_url: null,
}

const suggestion: MrpSuggestionRow = {
  id: SUGGESTION_ID,
  run_id: RUN_ID,
  item_id: ITEM_ID,
  item_code: 'BRASS-ROD-12',
  suggestion_type: 'JOB_WORK',
  required_quantity: '10.0000',
  available_quantity: '0.0000',
  incoming_quantity: '0.0000',
  shortage_quantity: '10.0000',
  suggested_quantity: '10.0000',
  required_date: REQUIRED_DATE,
  suggested_order_date: '2026-10-20',
  is_late: false,
  lead_time_days: 5,
  low_level_code: 0,
  expected_available_date: REQUIRED_DATE,
  from_warehouse_id: null,
  to_warehouse_id: null,
  from_reservation_id: null,
  from_demand_type: null,
  from_demand_id: null,
  from_demand_code: null,
  to_demand_type: null,
  to_demand_id: null,
  to_demand_line_id: null,
  to_demand_code: null,
  to_demand_priority: null,
  status: 'OPEN',
  converted_to_type: null,
  converted_to_id: null,
  converted_at: null,
  ignored_reason: null,
  source_demands: [],
  demand_provenance: null,
}

const requirement: MrpRequirementRow = {
  id: '01a00000-0000-7000-8000-00000000req1',
  run_id: RUN_ID,
  item_id: ITEM_ID,
  item_code: 'BRASS-ROD-12',
  level: 0,
  low_level_code: 0,
  direct_demand_quantity: '80.0000',
  dependent_demand_quantity: '0.0000',
  gross_requirement: '80.0000',
  on_hand_quantity: '0.0000',
  reserved_quantity: '0.0000',
  available_quantity: '0.0000',
  incoming_quantity: '0.0000',
  in_production_quantity: '0.0000',
  job_work_quantity: '25.0000',
  late_incoming_quantity: '0.0000',
  late_in_production_quantity: '0.0000',
  late_job_work_quantity: '10.0000',
  first_late_incoming_date: null,
  first_late_in_production_date: null,
  first_late_job_work_date: '2026-12-01',
  last_late_incoming_date: null,
  last_late_in_production_date: null,
  last_late_job_work_date: '2026-12-15',
  first_late_supply_date: '2026-12-01',
  net_requirement: '55.0000',
  planned_reallocation_quantity: '0.0000',
  shortage_quantity: '55.0000',
  earliest_required_date: REQUIRED_DATE,
  expected_available_date: REQUIRED_DATE,
  source_demand_count: 1,
  source_demands: [],
  demand_provenance: null,
  late_supply_quantity: '10.0000',
}

describe('MRP job-work conversion', () => {
  it('sends the planner inputs to the conversion endpoint', async () => {
    const { fetch, user } = renderScreen(<MrpRunDetailPage />, {
      route: `/mrp/runs/${RUN_ID}`,
      path: '/mrp/runs/:runId',
      permissions: [P.MRP_VIEW, P.MRP_CONVERT_SUGGESTION, P.VENDOR_VIEW],
      routes: {
        [`GET /mrp/runs/${RUN_ID}`]: ok(run),
        [`GET /mrp/runs/${RUN_ID}/suggestions`]: page([suggestion]),
        'GET /mrp/shortage-report': page([]),
        'GET /vendors/options': ok([
          { id: VENDOR_ID, code: 'V-ACME', name: 'Acme Finishing', is_approved: true },
        ]),
        [`POST /mrp/suggestions/${SUGGESTION_ID}/convert`]: ok({
          handled: true,
          message: 'Job-work order created',
          created_document_id: '01a00000-0000-7000-8000-00000000jwo1',
          created_document_code: 'JWO-0001',
          created_document_type: 'JOB_WORK_ORDER',
          suggestion: { ...suggestion, status: 'CONVERTED' },
        }),
      },
    })

    // The run must settle before its dependent suggestions query is enabled.
    await screen.findByRole('heading', { name: run.code })
    await waitFor(() => fetch.requestFor('GET', `/mrp/runs/${RUN_ID}/suggestions`))
    await user.click(await screen.findByRole('checkbox', { name: 'Select BRASS-ROD-12' }))
    await user.click(screen.getByRole('button', { name: 'Convert selected' }))

    const vendorSelect = screen.getByRole('combobox', { name: 'Approved vendor' })
    await user.click(vendorSelect)
    await user.click(await screen.findByRole('option', { name: 'V-ACME — Acme Finishing' }))

    const processSelect = screen.getByRole('combobox', { name: 'Process' })
    await user.click(processSelect)
    await user.click(await screen.findByRole('option', { name: 'Polishing' }))

    await user.click(screen.getByRole('button', { name: 'Create draft order' }))

    await waitFor(() => {
      expect(
        fetch.calls.some(
          (call) =>
            call.method === 'POST' && call.path === `/mrp/suggestions/${SUGGESTION_ID}/convert`,
        ),
      ).toBe(true)
    })
    expect(fetch.requestFor('POST', `/mrp/suggestions/${SUGGESTION_ID}/convert`)).toEqual({
      target: 'JOB_WORK_ORDER',
      vendor_id: VENDOR_ID,
      process_type: 'POLISHING',
      expected_return_date: REQUIRED_DATE,
      warehouse_id: WAREHOUSE_ID,
    })
  })
})

describe('MRP transfer conversion', () => {
  it('creates a stock transfer from the suggestion route', async () => {
    const transferSuggestion: MrpSuggestionRow = {
      ...suggestion,
      id: TRANSFER_SUGGESTION_ID,
      suggestion_type: 'TRANSFER',
      from_warehouse_id: SOURCE_WAREHOUSE_ID,
      to_warehouse_id: WAREHOUSE_ID,
    }
    const { fetch, user } = renderScreen(<MrpRunDetailPage />, {
      route: `/mrp/runs/${RUN_ID}`,
      path: '/mrp/runs/:runId',
      permissions: [P.MRP_VIEW, P.MRP_CONVERT_SUGGESTION, P.WAREHOUSE_VIEW],
      routes: {
        [`GET /mrp/runs/${RUN_ID}`]: ok({
          ...run,
          warehouse_ids: [SOURCE_WAREHOUSE_ID, WAREHOUSE_ID],
        }),
        [`GET /mrp/runs/${RUN_ID}/suggestions`]: page([transferSuggestion]),
        'GET /mrp/shortage-report': page([]),
        'GET /warehouses': page([
          { id: SOURCE_WAREHOUSE_ID, code: 'WH-A', name: 'Source' },
          { id: WAREHOUSE_ID, code: 'WH-B', name: 'Destination' },
        ]),
        [`POST /mrp/suggestions/${TRANSFER_SUGGESTION_ID}/convert`]: ok({
          handled: true,
          message: 'Stock transfer created',
          created_document_id: '01a00000-0000-7000-8000-00000000trn1',
          created_document_code: 'TRN-0001',
          created_document_type: 'STOCK_TRANSFER',
          suggestion: { ...transferSuggestion, status: 'CONVERTED' },
        }),
      },
    })

    await screen.findByRole('heading', { name: run.code })
    expect(await screen.findByText('WH-A → WH-B')).toBeInTheDocument()
    await user.click(await screen.findByRole('checkbox', { name: 'Select BRASS-ROD-12' }))
    await user.click(screen.getByRole('button', { name: 'Convert selected' }))
    await user.click(await screen.findByRole('button', { name: /^Convert$/ }))

    await waitFor(() => {
      expect(
        fetch.calls.some(
          (call) =>
            call.method === 'POST' &&
            call.path === `/mrp/suggestions/${TRANSFER_SUGGESTION_ID}/convert`,
        ),
      ).toBe(true)
    })
    expect(fetch.requestFor('POST', `/mrp/suggestions/${TRANSFER_SUGGESTION_ID}/convert`)).toEqual(
      {},
    )
  })
})

describe('MRP reallocation conversion', () => {
  it('shows the donor-to-demand route and converts it to a request', async () => {
    const reallocationSuggestion: MrpSuggestionRow = {
      ...suggestion,
      id: REALLOCATION_SUGGESTION_ID,
      suggestion_type: 'REALLOCATION',
      from_warehouse_id: WAREHOUSE_ID,
      to_warehouse_id: WAREHOUSE_ID,
      from_reservation_id: '01a00000-0000-7000-8000-00000000res1',
      from_demand_type: 'SALES_ORDER',
      from_demand_id: '01a00000-0000-7000-8000-00000000don1',
      from_demand_code: 'SO/DONOR',
      to_demand_type: 'SALES_ORDER',
      to_demand_id: '01a00000-0000-7000-8000-00000000rec1',
      to_demand_line_id: '01a00000-0000-7000-8000-00000000lin1',
      to_demand_code: 'SO/RECIPIENT',
      to_demand_priority: 2,
    }
    const { fetch, user } = renderScreen(<MrpRunDetailPage />, {
      route: `/mrp/runs/${RUN_ID}`,
      path: '/mrp/runs/:runId',
      permissions: [P.MRP_VIEW, P.MRP_CONVERT_SUGGESTION, P.ALLOCATION_VIEW],
      routes: {
        [`GET /mrp/runs/${RUN_ID}`]: ok(run),
        [`GET /mrp/runs/${RUN_ID}/suggestions`]: page([reallocationSuggestion]),
        'GET /mrp/shortage-report': page([]),
        [`POST /mrp/suggestions/${REALLOCATION_SUGGESTION_ID}/convert`]: ok({
          handled: true,
          message: 'Reallocation request created',
          created_document_id: '01a00000-0000-7000-8000-00000000rea1',
          created_document_code: 'SO/RECIPIENT',
          created_document_type: 'REALLOCATION_REQUEST',
          suggestion: { ...reallocationSuggestion, status: 'CONVERTED' },
        }),
      },
    })

    await screen.findByRole('heading', { name: run.code })
    expect(await screen.findByText('SO/DONOR → SO/RECIPIENT')).toBeInTheDocument()
    await user.click(await screen.findByRole('checkbox', { name: 'Select BRASS-ROD-12' }))
    await user.click(screen.getByRole('button', { name: 'Convert selected' }))
    await user.click(await screen.findByRole('button', { name: /^Convert$/ }))

    await waitFor(() => {
      expect(
        fetch.calls.some(
          (call) =>
            call.method === 'POST' &&
            call.path === `/mrp/suggestions/${REALLOCATION_SUGGESTION_ID}/convert`,
        ),
      ).toBe(true)
    })
    expect(
      fetch.requestFor('POST', `/mrp/suggestions/${REALLOCATION_SUGGESTION_ID}/convert`),
    ).toEqual({})
  })
})

describe('MRP shortage supply', () => {
  it('shows job-work output and its late interval on the requirement row', async () => {
    const { fetch } = renderScreen(<MrpRunDetailPage />, {
      route: `/mrp/runs/${RUN_ID}`,
      path: '/mrp/runs/:runId',
      permissions: [P.MRP_VIEW],
      routes: {
        [`GET /mrp/runs/${RUN_ID}`]: ok(run),
        [`GET /mrp/runs/${RUN_ID}/suggestions`]: page([]),
        'GET /mrp/shortage-report': page([requirement]),
      },
    })

    // The shortage panel is mounted only after the parent run has loaded.
    await screen.findByRole('heading', { name: run.code })
    await waitFor(() => fetch.requestFor('GET', '/mrp/shortage-report'))
    const row = (await screen.findByText('BRASS-ROD-12')).closest('tr')
    expect(row).not.toBeNull()
    const cells = within(row as HTMLElement)
    expect(cells.getByText('25')).toBeInTheDocument()
    expect(cells.getByText('10')).toBeInTheDocument()
    expect(cells.getByText(/01 Dec 2026/)).toBeInTheDocument()
    expect(cells.getByText(/15 Dec 2026/)).toBeInTheDocument()
  })
})
