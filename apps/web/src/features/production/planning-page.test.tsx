/**
 * The production planning board.
 *
 * A week of capacity and operations, drawn from the two board endpoints, with
 * a reschedule that goes through the assignment dialog's PATCH - which is what
 * re-validates the window and warns about a double-booked machine.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen, type RouteTable } from '@/test/harness'

import { localTodayISO } from './planning-model'
import PlanningPage from './planning-page'
import type { WorkOrder } from './work-order-model'

const TODAY = localTodayISO()

const CENTRE = { id: 'wc-1', code: 'MILL-01', name: 'CNC milling' }

const MACHINE = {
  id: 'mc-1',
  code: 'MC-01',
  name: 'Mill 1',
  work_center_id: CENTRE.id,
  machine_type: null,
  capacity_per_hour: '10.0000',
  cost_per_hour: '100.0000',
  status: 'AVAILABLE',
  last_maintenance_date: null,
  next_maintenance_date: null,
  is_active: true,
  created_at: '2026-01-01T09:00:00Z',
  updated_at: '2026-01-01T09:00:00Z',
}

function workOrder(overrides: Partial<WorkOrder> = {}): WorkOrder {
  return {
    id: 'wo-1',
    code: 'WO-0001',
    production_order_id: 'po-1',
    sequence: 10,
    operation_code: 'MILL',
    operation_name: 'Mill the face',
    work_center_id: CENTRE.id,
    machine_id: null,
    operator_user_id: null,
    status: 'PENDING',
    planned_quantity: '10.0000',
    completed_quantity: '0.0000',
    rejected_quantity: '0.0000',
    planned_setup_minutes: '30.0000',
    planned_run_minutes: '60.0000',
    planned_start_at: `${TODAY}T06:00:00Z`,
    planned_end_at: `${TODAY}T07:30:00Z`,
    actual_start_at: null,
    actual_end_at: null,
    shift_id: null,
    shift: null,
    notes: null,
    work_center_cost_per_hour: '250.0000',
    work_center_setup_cost: '100.0000',
    process_cost: '0.0000',
    is_outsourced: false,
    ...overrides,
  }
}

function capacityRow(overrides: Record<string, unknown> = {}) {
  return {
    work_center_id: CENTRE.id,
    work_center_code: CENTRE.code,
    work_center_name: CENTRE.name,
    date: TODAY,
    planned_minutes: '90.0000',
    capacity_minutes: '480.0000',
    capacity_source: 'STANDARD',
    over_capacity: false,
    ...overrides,
  }
}

function board(routes: RouteTable = {}) {
  return renderScreen(<PlanningPage />, {
    permissions: [P.PRODUCTION_ORDER_VIEW, P.WORK_ORDER_MANAGE],
    routes: {
      'GET /production/capacity': page([capacityRow()]),
      'GET /production/work-orders': page([workOrder()]),
      'GET /routing/work-centers': page([CENTRE]),
      'GET /routing/machines': page([MACHINE]),
      ...routes,
    },
  })
}

describe('the planning board', () => {
  it('draws the week load and the operations on it, flagging overloads', async () => {
    const seen: URL[] = []
    board({
      'GET /production/capacity': ({ url }) => {
        seen.push(url)
        return page([capacityRow({ planned_minutes: '600.0000', over_capacity: true })])
      },
      'GET /production/work-orders': ({ url }) => {
        seen.push(url)
        return page([workOrder()])
      },
    })

    expect(await screen.findByText('Load against capacity')).toBeVisible()
    expect(await screen.findByText('Over')).toBeVisible()
    expect(await screen.findByRole('button', { name: /reschedule mill the face/i })).toBeVisible()

    // The board asks for the visible week on both endpoints.
    const capacityCall = seen.find((url) => url.pathname.endsWith('/production/capacity'))
    expect(capacityCall?.searchParams.get('date_from')).toBe(TODAY)
    const boardCall = seen.find((url) => url.pathname.endsWith('/production/work-orders'))
    expect(boardCall?.searchParams.get('open_only')).toBe('true')
    expect(boardCall?.searchParams.get('planned_to')).toBeTruthy()
  })

  it('reschedules through the assignment dialog, which re-validates the window', async () => {
    const { user, fetch } = board({
      'PATCH /production/work-orders/wo-1': ok(workOrder({ notes: 'Moved to Tuesday' })),
    })

    await user.click(await screen.findByRole('button', { name: /reschedule mill the face/i }))
    const dialog = await screen.findByRole('dialog')
    const notes = within(dialog).getByRole('textbox', { name: 'Notes' })
    await user.type(notes, 'Moved to Tuesday')
    await user.click(within(dialog).getByRole('button', { name: /save assignment/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('PATCH', '/production/work-orders/wo-1'),
    )) as Record<string, unknown>
    expect(body).toEqual({ notes: 'Moved to Tuesday' })
  })

  it('shows a clash badge when two loaded operations share a machine and an hour', async () => {
    board({
      'GET /production/work-orders': page([
        workOrder({ id: 'wo-1', machine_id: 'mc-1' }),
        workOrder({
          id: 'wo-2',
          code: 'WO-0002',
          operation_name: 'Drill',
          machine_id: 'mc-1',
          planned_start_at: `${TODAY}T07:00:00Z`,
          planned_end_at: `${TODAY}T08:00:00Z`,
        }),
      ]),
    })

    expect(await screen.findAllByText('Machine clash')).toHaveLength(2)
  })
})
