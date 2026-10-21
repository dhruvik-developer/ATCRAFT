/**
 * The assembly board: load per line and the late orders grouped under it.
 *
 * The counts come from the grouped `board/lines` read, never from the page of
 * rows below it, and each late order carries the server's own lateness badge -
 * days past the planned end, or a start overdue. Opening a row lands on the
 * same order panel as the register.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import type { AssemblyComponent } from './assembly-model'
import AssemblyPage from './assembly-page'

const LINES = [
  {
    assembly_line: 'Line 1',
    open_orders: 3,
    not_started_orders: 1,
    in_progress_orders: 1,
    qc_pending_orders: 1,
    late_orders: 2,
    planned_quantity: '30.0000',
    earliest_planned_start: '2026-03-01',
    latest_planned_end: '2026-03-04',
  },
  {
    assembly_line: null,
    open_orders: 1,
    not_started_orders: 1,
    in_progress_orders: 0,
    qc_pending_orders: 0,
    late_orders: 0,
    planned_quantity: '5.0000',
    earliest_planned_start: null,
    latest_planned_end: null,
  },
]

const ORDERS = [
  {
    id: 'asm-late-1',
    code: 'ASM-0101',
    item_id: 'item-aaa-0001',
    status: 'IN_PROGRESS',
    assembly_line: 'Line 1',
    warehouse_id: 'wh-1',
    output_warehouse_id: 'wh-2',
    priority: 20,
    planned_quantity: '10.0000',
    assembled_quantity: '4.0000',
    rejected_quantity: '0.0000',
    planned_start: '2026-03-01',
    planned_end: '2026-03-03',
    actual_start: '2026-03-02T09:00:00Z',
    actual_end: null,
    requires_qc: false,
    is_late: true,
    start_overdue: false,
    days_late: 3,
  },
  {
    id: 'asm-late-2',
    code: 'ASM-0102',
    item_id: 'item-bbb-0001',
    status: 'RELEASED',
    assembly_line: 'Line 1',
    warehouse_id: 'wh-1',
    output_warehouse_id: 'wh-2',
    priority: 100,
    planned_quantity: '20.0000',
    assembled_quantity: '0.0000',
    rejected_quantity: '0.0000',
    planned_start: '2026-02-20',
    planned_end: '2026-03-04',
    actual_start: null,
    actual_end: null,
    requires_qc: false,
    is_late: false,
    start_overdue: true,
    days_late: 0,
  },
]

const DETAIL = {
  ...ORDERS[0],
  version: 1,
  bom_id: 'bom-1',
  qc_inspection_id: null,
  qc_held_at: null,
  output_batch_id: null,
  output_serial_ids: null,
  packing_order_id: null,
  packed_at: null,
  source_type: 'MANUAL',
  source_id: null,
  cancellation_reason: null,
  notes: null,
}

const COMPONENT: AssemblyComponent = {
  id: 'ac-1',
  assembly_order_id: 'asm-late-1',
  line_number: 1,
  item_id: 'item-bbb-0001',
  uom_id: 'uom-1',
  required_quantity: '100.0000',
  issued_quantity: '100.0000',
  consumed_quantity: '0.0000',
  is_serial_tracked_component: false,
  is_batch_tracked_component: false,
  issued_batch_id: null,
  consumed_batch_id: null,
  consumed_serial_ids: [],
  reservation_id: null,
  notes: null,
}

describe('the assembly board', () => {
  async function openBoard() {
    const rendered = renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW, P.ASSEMBLY_ORDER_MANAGE],
      routes: {
        'GET /assembly': page([]),
        'GET /assembly/board/lines': page(LINES),
        'GET /assembly/board/orders': page(ORDERS),
      },
    })
    await rendered.user.click(screen.getByRole('tab', { name: 'Board' }))
    return rendered
  }

  it('counts the load per line without counting the rows below it', async () => {
    await openBoard()

    // Aggregated server-side: a busy line is counted whole even when its
    // orders spill across pages of the list beneath.
    expect(await screen.findByText('Line 1')).toBeVisible()
    expect(screen.getByText(/3 open/)).toBeVisible()
    expect(screen.getByText('No line assigned')).toBeVisible()
  })

  it('groups the late orders by line with the server worked lateness', async () => {
    await openBoard()

    expect(await screen.findByText('ASM-0101')).toBeVisible()
    expect(screen.getByText('3d late')).toBeVisible()
    expect(screen.getByText('Start overdue')).toBeVisible()
  })

  it('opens the same order panel as the register', async () => {
    const rendered = renderScreen(<AssemblyPage />, {
      permissions: [P.ASSEMBLY_ORDER_VIEW, P.ASSEMBLY_ORDER_MANAGE],
      routes: {
        'GET /assembly': page([]),
        'GET /assembly/board/lines': page(LINES),
        'GET /assembly/board/orders': page(ORDERS),
        'GET /assembly/asm-late-1': ok(DETAIL),
        'GET /assembly/asm-late-1/components': ok([COMPONENT]),
      },
    })
    await rendered.user.click(screen.getByRole('tab', { name: 'Board' }))
    await rendered.user.click(await screen.findByText('ASM-0101'))

    // The panel reads the full header, which the board row does not carry.
    expect(await screen.findByRole('dialog')).toBeVisible()
  })

  it('pages line loads and late orders independently without inheriting register filters', async () => {
    const lineRequests: URL[] = []
    const orderRequests: URL[] = []
    const rendered = renderScreen(<AssemblyPage />, {
      route: '/assembly?page=7&q=unrelated&sort=code&dir=desc',
      permissions: [P.ASSEMBLY_ORDER_VIEW],
      routes: {
        'GET /assembly': page([]),
        'GET /assembly/board/lines': ({ url }) => {
          lineRequests.push(url)
          const current = Number(url.searchParams.get('page'))
          return page([{ ...LINES[0], assembly_line: `Load line ${current}` }], {
            page: current,
            total_pages: 2,
            has_next: current === 1,
            has_previous: current === 2,
          })
        },
        'GET /assembly/board/orders': ({ url }) => {
          orderRequests.push(url)
          const current = Number(url.searchParams.get('page'))
          return page([{ ...ORDERS[0], id: `late-${current}`, code: `Late order ${current}` }], {
            page: current,
            total_pages: 2,
            has_next: current === 1,
            has_previous: current === 2,
          })
        },
      },
    })
    await rendered.user.click(screen.getByRole('tab', { name: 'Board' }))
    expect(await screen.findByText('Load line 1')).toBeVisible()
    expect(await screen.findByText('Late order 1')).toBeVisible()
    expect(lineRequests[0]?.searchParams.has('search')).toBe(false)
    expect(orderRequests[0]?.searchParams.has('sort_by')).toBe(false)

    await rendered.user.click(
      within(screen.getByRole('navigation', { name: 'Assembly lines pagination' })).getByRole(
        'button',
        { name: 'Next' },
      ),
    )
    expect(await screen.findByText('Load line 2')).toBeVisible()
    expect(screen.getByText('Late order 1')).toBeVisible()
    expect(orderRequests).toHaveLength(1)

    await rendered.user.click(
      within(screen.getByRole('navigation', { name: 'Late orders pagination' })).getByRole(
        'button',
        { name: 'Next' },
      ),
    )
    expect(await screen.findByText('Late order 2')).toBeVisible()
    expect(screen.getByText('Load line 2')).toBeVisible()
    await waitFor(() => expect(lineRequests).toHaveLength(2))
    expect(orderRequests.at(-1)?.searchParams.get('late_only')).toBe('true')
  })
})
