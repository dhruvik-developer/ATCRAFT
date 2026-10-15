/**
 * Production order detail.
 *
 * What is pinned here is the cancellation's audit reason. `CancelRequest.reason`
 * is bounded 3..255 characters on the API, and a 422 keyed on a field is one
 * the query client stays quiet about on the assumption the form renders it -
 * so a reason the form accepted but the API refused made Cancel do nothing at
 * all, with no message anywhere.
 */

import { screen, waitFor } from '@testing-library/react'
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
