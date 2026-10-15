/**
 * Production order detail.
 *
 * What is pinned here is the cancellation's audit reason. `CancelRequest.reason`
 * is bounded 3..255 characters on the API, and a 422 keyed on a field is one
 * the query client stays quiet about on the assumption the form renders it -
 * so a reason the form accepted but the API refused made Cancel do nothing at
 * all, with no message anywhere.
 */

import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import ProductionDetailPage from './production-detail-page'
import type { ProductionOrderDetail } from './production-model'

const ORDER_ID = '01a00000-0000-7000-8000-0000000000p1'

/** `GET /production/{id}` - a draft, which is the state cancellation is offered from. */
function detail(overrides: Partial<ProductionOrderDetail> = {}): ProductionOrderDetail {
  return {
    id: ORDER_ID,
    code: 'PRD-0001',
    status: 'DRAFT',
    item_id: '01a00000-0000-7000-8000-0000000000i1',
    warehouse_id: '01a00000-0000-7000-8000-0000000000w1',
    plant_id: null,
    cost_centre_id: null,
    planned_quantity: '100.0000',
    produced_quantity: '0.0000',
    rejected_quantity: '0.0000',
    scrap_quantity: '0.0000',
    planned_start_date: '2026-09-08',
    planned_end_date: '2026-09-12',
    actual_start_date: null,
    actual_end_date: null,
    priority: 100,
    source_type: 'MANUAL',
    source_id: null,
    requires_qc: false,
    created_at: '2026-09-01T06:00:00Z',
    bom_id: null,
    routing_id: null,
    approved_by_id: null,
    approved_at: null,
    closure_note: null,
    notes: null,
    total_material_cost: '0.00',
    total_operation_cost: '0.00',
    total_cost: '0.00',
    version: 1,
    updated_at: '2026-09-01T06:00:00Z',
    ...overrides,
  }
}

/** Render the detail screen for `ORDER_ID`, with the route param bound. */
function renderDetail(permissions: string[], routes: Record<string, unknown> = {}) {
  return renderScreen(<ProductionDetailPage />, {
    permissions,
    route: `/production/${ORDER_ID}`,
    path: '/production/:orderId',
    routes: {
      [`GET /production/${ORDER_ID}`]: ok(detail()),
      [`GET /production/${ORDER_ID}/components`]: ok([]),
      ...routes,
    },
  })
}

describe('completing an order', () => {
  it('captures final output, rejection reason, scrap, date, shift and note', async () => {
    const inProgress = detail({
      status: 'IN_PROCESS',
      produced_quantity: '65.0000',
      rejected_quantity: '2.0000',
      scrap_quantity: '1.0000',
    })
    const { user, fetch } = renderScreen(<ProductionDetailPage />, {
      permissions: [P.PRODUCTION_ORDER_VIEW, P.PRODUCTION_ENTRY_CREATE],
      route: `/production/${ORDER_ID}`,
      path: '/production/:orderId',
      routes: {
        [`GET /production/${ORDER_ID}`]: ok(inProgress),
        [`GET /production/${ORDER_ID}/components`]: ok([]),
        'GET /routing/shifts': page([
          {
            id: 'shift-day',
            code: 'DAY',
            name: 'Day shift',
            start_time: '08:00:00',
            end_time: '16:00:00',
            crosses_midnight: false,
            break_minutes: 30,
            sort_order: 1,
            is_active: true,
          },
        ]),
        [`POST /production/${ORDER_ID}/complete`]: ok({
          message: 'Production completed.',
          order: detail({ status: 'COMPLETED', actual_end_date: '2026-10-15' }),
          output_movement: null,
          backflush_issue_code: null,
          backflush_movements: [],
          consumed_component_count: 0,
        }),
      },
    })

    await user.click(await screen.findByRole('button', { name: /^complete$/i }))
    expect(await screen.findByText('Planned against booked')).toBeVisible()
    expect(screen.getByText('65')).toBeVisible()
    fireEvent.change(screen.getByLabelText('Final good output'), {
      target: { value: '30.2500' },
    })
    fireEvent.change(screen.getByLabelText('Rejected quantity'), { target: { value: '3' } })
    await user.click(screen.getByRole('button', { name: 'Complete order' }))

    expect(await screen.findByText('A rejection needs a reason')).toBeVisible()
    expect(fetch.calls.some((call) => call.path.endsWith('/complete'))).toBe(false)
    fireEvent.change(screen.getByLabelText('Rejection reason'), {
      target: { value: 'Dimension out of tolerance' },
    })
    fireEvent.change(screen.getByLabelText('Scrap quantity'), { target: { value: '1.25' } })
    fireEvent.change(screen.getByLabelText(/Actual end date/), {
      target: { value: '2026-10-14' },
    })
    await user.click(screen.getByRole('combobox', { name: 'Shift' }))
    await user.click(await screen.findByRole('option', { name: /DAY/ }))
    fireEvent.change(screen.getByLabelText('Completion note'), {
      target: { value: 'Final inspection complete' },
    })
    await user.click(screen.getByRole('button', { name: 'Complete order' }))

    const body = (await waitFor(() =>
      fetch.requestFor('POST', `/production/${ORDER_ID}/complete`),
    )) as Record<string, unknown>
    expect(body).toEqual({
      produced_quantity: '30.2500',
      rejected_quantity: '3',
      rejection_reason: 'Dimension out of tolerance',
      scrap_quantity: '1.25',
      entry_date: '2026-10-14',
      actual_end_date: '2026-10-14',
      shift_id: 'shift-day',
      note: 'Final inspection complete',
    })
  })
})

describe('closing an order', () => {
  it('shows planned and booked figures and sends the closure note', async () => {
    const completed = detail({
      status: 'COMPLETED',
      produced_quantity: '92.0000',
      rejected_quantity: '4.0000',
      scrap_quantity: '2.0000',
    })
    const { user, fetch } = renderScreen(<ProductionDetailPage />, {
      permissions: [P.PRODUCTION_ORDER_VIEW, P.PRODUCTION_ORDER_CLOSE],
      route: `/production/${ORDER_ID}`,
      path: '/production/:orderId',
      routes: {
        [`GET /production/${ORDER_ID}`]: ok(completed),
        [`GET /production/${ORDER_ID}/components`]: ok([]),
        [`POST /production/${ORDER_ID}/close`]: ok({
          message: 'Production order closed.',
          order: detail({ status: 'CLOSED', closure_note: 'Variance reviewed' }),
          cost: {
            production_order_id: ORDER_ID,
            code: 'PRD-0001',
            status: 'CLOSED',
            material_cost: '0.00',
            operation_cost: '0.00',
            total_cost: '0.00',
            cost_per_unit: null,
            planned_quantity: '100.0000',
            produced_quantity: '92.0000',
            is_final: true,
            uncosted_operation_count: 0,
          },
        }),
      },
    })

    await user.click(await screen.findByRole('button', { name: /^close$/i }))
    expect(await screen.findByText('Planned against booked')).toBeVisible()
    expect(screen.getByText('92')).toBeVisible()
    fireEvent.change(screen.getByLabelText('Closure note'), {
      target: { value: 'Variance reviewed' },
    })
    await user.click(screen.getByRole('button', { name: 'Close order' }))

    const body = (await waitFor(() =>
      fetch.requestFor('POST', `/production/${ORDER_ID}/close`),
    )) as Record<string, unknown>
    expect(body).toEqual({ closure_note: 'Variance reviewed' })
  })
})

describe('production costing', () => {
  it('renders the material and operation cost breakdowns', async () => {
    const { user } = renderDetail([P.PRODUCTION_ORDER_VIEW, P.PRODUCTION_COST_VIEW], {
      [`GET /production/${ORDER_ID}/cost`]: ok({
        production_order_id: ORDER_ID,
        code: 'PRD-0001',
        status: 'IN_PROCESS',
        is_final: false,
        planned_quantity: '100.0000',
        produced_quantity: '40.0000',
        material_cost: '21.00',
        operation_cost: '12.50',
        total_cost: '33.50',
        cost_per_unit: '0.84',
        uncosted_operation_count: 0,
        materials: [
          {
            component_id: '01a00000-0000-7000-8000-0000000000c1',
            item_id: '01a00000-0000-7000-8000-0000000000i2',
            item_code: 'MAT-001',
            required_quantity: '2.0000',
            issued_quantity: '2.5000',
            returned_quantity: '0.5000',
            net_issued_quantity: '2.0000',
            unit_cost: '10.5000',
            total_cost: '21.00',
          },
        ],
        operations: [
          {
            work_order_id: '01a00000-0000-7000-8000-0000000000o1',
            sequence: 10,
            operation_code: 'OP-10',
            work_center_id: '01a00000-0000-7000-8000-0000000000w2',
            status: 'COMPLETED',
            charged_minutes: '15.0000',
            is_actual_time: true,
            cost_per_hour: '40.0000',
            time_cost: '10.00',
            setup_cost: '2.50',
            process_cost: '0.00',
            total_cost: '12.50',
          },
        ],
      }),
    })

    await user.click(await screen.findByRole('tab', { name: /costing/i }))

    expect(await screen.findByRole('heading', { name: 'Material cost by component' })).toBeVisible()
    expect(screen.getByText('MAT-001')).toBeVisible()
    expect(screen.getByText('2.5')).toBeVisible()
    expect(screen.getByText('0.5')).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Operation cost by work order' })).toBeVisible()
    expect(screen.getByText('OP-10')).toBeVisible()
    expect(screen.getAllByText('Completed')).toHaveLength(2)
    expect(screen.getByText('15')).toBeVisible()
  })
})

describe('cancelling an order', () => {
  it('records the audit reason the API insists on, within its bounds', async () => {
    const { user, fetch } = renderDetail([P.PRODUCTION_ORDER_VIEW, P.PRODUCTION_ORDER_CANCEL], {
      [`POST /production/${ORDER_ID}/cancel`]: ok(detail({ status: 'CANCELLED' })),
    })

    await user.click(await screen.findByRole('button', { name: /^cancel$/i }))
    await user.click(await screen.findByRole('button', { name: /cancel order/i }))

    // Empty: caught here rather than becoming a 422 nobody sees.
    expect(await screen.findByText(/needs a recorded reason/i)).toBeVisible()
    expect(fetch.calls.some((call) => call.path.endsWith('/cancel'))).toBe(false)

    // Two characters: pydantic's floor is 3, and the 422 it answers with is
    // keyed on `reason` - which the query client suppresses on the assumption
    // the form shows it. Until the floor was enforced here, Cancel did nothing.
    await user.type(screen.getByLabelText(/reason/i), 'no')
    await user.click(screen.getByRole('button', { name: /cancel order/i }))
    expect(await screen.findByText(/at least 3 characters/i)).toBeVisible()
    expect(fetch.calls.some((call) => call.path.endsWith('/cancel'))).toBe(false)

    await user.clear(screen.getByLabelText(/reason/i))
    await user.type(screen.getByLabelText(/reason/i), 'Customer withdrew the order')
    await user.click(screen.getByRole('button', { name: /cancel order/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('POST', `/production/${ORDER_ID}/cancel`),
    )) as Record<string, unknown>
    expect(body).toEqual({ reason: 'Customer withdrew the order' })
  })

  it('withholds the cancellation from an account without production_order.cancel', async () => {
    renderDetail([P.PRODUCTION_ORDER_VIEW])

    expect(await screen.findByRole('heading', { name: 'PRD-0001' })).toBeVisible()
    expect(screen.queryByRole('button', { name: /^cancel$/i })).toBeNull()
  })
})

describe('where the order is charged', () => {
  const MACHINING = {
    id: 'cc-mach',
    code: 'CC-MACH',
    name: 'Machining',
    cost_centre_type: 'PRODUCTION',
    parent_id: null,
    plant_id: null,
    head_user_id: null,
    description: null,
    // Switched off since the order was raised: the order keeps it, and says so.
    is_active: false,
    created_at: '2026-09-01T06:00:00Z',
    updated_at: '2026-09-01T06:00:00Z',
  }

  it('names the cost centre, inactive or not, to an account that may read centres', async () => {
    renderDetail([P.PRODUCTION_ORDER_VIEW, P.COST_CENTRE_VIEW], {
      [`GET /production/${ORDER_ID}`]: ok(detail({ cost_centre_id: MACHINING.id })),
      'GET /organization/cost-centres': page([MACHINING]),
    })

    expect(await screen.findByText('CC-MACH - Machining (inactive)')).toBeVisible()
  })

  it('shows no cost-centre row, and asks for no centre, without the right', async () => {
    const { fetch } = renderDetail([P.PRODUCTION_ORDER_VIEW], {
      [`GET /production/${ORDER_ID}`]: ok(detail({ cost_centre_id: MACHINING.id })),
    })

    expect(await screen.findByRole('heading', { name: 'PRD-0001' })).toBeVisible()
    expect(screen.queryByText('Cost centre')).toBeNull()
    expect(fetch.calls.some((call) => call.path.startsWith('/organization/cost-centres'))).toBe(
      false,
    )
  })
})
