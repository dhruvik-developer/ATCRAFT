/**
 * The shop-floor queue.
 *
 * Two properties carry this file.
 *
 * **The row menu is the state machine.** `WORK_ORDER_FLOW` has exactly two
 * edges out of PENDING and two out of IN_PROGRESS, and none at all out of the
 * terminal states. The status endpoint accepts any member of `WorkOrderStatus`,
 * so a screen that offered all four would compile, look complete, and put an
 * operator through a confirmation only to be refused by `assert_transition` at
 * the end of it. Each status is asserted for the exact set of actions it offers,
 * the empty set included.
 *
 * **Both write paths want `work_order.manage`.** Not the view right the page is
 * reached with, and not any production-order right. Every gate is rendered twice,
 * once with the right and once without: a `can()` stubbed to true would have
 * passed every authorisation defect this codebase has shipped.
 *
 * Two mechanical notes, because both cost real time to find.
 *
 * `queueSettled()` is not ceremony. The column definitions are memoised on the
 * two label lookups, so each one landing rebuilds every cell in the grid - and a
 * node captured before that is detached by the time a click is dispatched, which
 * lands on nothing and fails as "the dialog never opened".
 *
 * The long timeouts are the environment, not the screen. Mounting a Radix
 * *Popper* overlay - the row menu is one - costs about twenty seconds of CPU
 * under jsdom every time it opens, before any assertion runs; a Dialog, which
 * uses no Popper, mounts in a hundred milliseconds. So the menu is opened as few
 * times as the assertions allow, and those tests fold the whole action - open,
 * choose, confirm, assert the request - into one opening.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import type { WorkOrder } from './work-order-model'
import WorkOrdersPage from './work-orders-page'

/** One Radix Popper mount, plus room for the rest of the test. */
const POPPER = 90_000

// ------------------------------------------------------------- fixtures ----

function workOrder(overrides: Partial<WorkOrder> = {}): WorkOrder {
  return {
    id: 'wo-1',
    code: 'WO-0001',
    production_order_id: 'po-1',
    sequence: 10,
    operation_code: 'DEBURR',
    operation_name: 'Deburr',
    work_center_id: 'wc-1',
    machine_id: null,
    operator_user_id: 'user-7',
    status: 'PENDING',
    planned_quantity: '100.0000',
    completed_quantity: '0.0000',
    rejected_quantity: '0.0000',
    planned_setup_minutes: '30.0000',
    planned_run_minutes: '90.0000',
    planned_start_at: '2026-02-02T06:00:00Z',
    planned_end_at: '2026-02-02T08:00:00Z',
    actual_start_at: null,
    actual_end_at: null,
    shift: null,
    notes: null,
    work_center_cost_per_hour: '250.0000',
    work_center_setup_cost: '100.0000',
    process_cost: '0.0000',
    ...overrides,
  }
}

/**
 * The two bounded lookups the queue resolves its labels from.
 *
 * Routed on every render, not because the assertions read them, but because both
 * soft-fail to an empty list: left unrouted, a screen that had started asking
 * for something else entirely would still look perfectly healthy.
 */
const LOOKUPS = {
  'GET /routing/work-centers': page([{ id: 'wc-1', code: 'MILL-01', name: 'CNC milling' }]),
  'GET /users': page([{ id: 'user-7', full_name: 'Rakesh Patel', email: 'rakesh@example.com' }]),
}

const queue = (rows: WorkOrder[]) => ({
  ...LOOKUPS,
  'GET /production/work-orders': page(rows),
})

/**
 * Both label lookups have landed, so the grid has stopped rebuilding its cells.
 *
 * The work-centre name and the operator's name each appear only once their own
 * lookup has resolved, which is exactly the event that invalidates the column
 * memo - so waiting for both is waiting for the last re-render.
 */
async function queueSettled(): Promise<void> {
  await screen.findByText('MILL-01 - CNC milling')
  await screen.findByText('Rakesh Patel')
}

/** The labels on the open row menu, in the order the screen offers them. */
function offeredActions(): string[] {
  return within(screen.getByRole('menu'))
    .getAllByRole('menuitem')
    .map((item) => item.textContent?.trim() ?? '')
}

// ---------------------------------------------------------- authorisation --

describe('who may touch an operation', () => {
  it('offers neither assignment nor a status change without work_order.manage', async () => {
    // `work_order.view` is what gets someone onto this page. It is not what lets
    // them start a job, and the API refuses both writes on it. Production-order
    // rights are handed out far more freely and are the wrong gate here.
    renderScreen(<WorkOrdersPage />, {
      permissions: [P.WORK_ORDER_VIEW, P.PRODUCTION_ORDER_VIEW, P.PRODUCTION_ORDER_APPROVE],
      routes: queue([workOrder()]),
    })
    await queueSettled()

    expect(screen.queryByRole('button', { name: 'Assign WO-0001' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Change the status of WO-0001' })).toBeNull()
  })

  it('offers both once the manage right is held', async () => {
    renderScreen(<WorkOrdersPage />, {
      permissions: [P.WORK_ORDER_VIEW, P.WORK_ORDER_MANAGE],
      routes: queue([workOrder()]),
    })
    await queueSettled()

    expect(screen.getByRole('button', { name: 'Assign WO-0001' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Change the status of WO-0001' })).toBeVisible()
  })
})

// --------------------------------------------------------- the lifecycle ---

describe('what each status offers', () => {
  it(
    'offers Start and Cancel from PENDING, and starts on a null time meaning "now"',
    async () => {
      // There is no PENDING -> COMPLETED edge. An operation done without anyone
      // starting it is started and then completed; the API stamps the missing
      // start from the completion moment so the row stays honest.
      const { user, fetch } = renderScreen(<WorkOrdersPage />, {
        permissions: [P.WORK_ORDER_VIEW, P.WORK_ORDER_MANAGE],
        routes: {
          ...queue([workOrder({ status: 'PENDING' })]),
          'POST /production/work-orders/wo-1/status': ok(workOrder({ status: 'IN_PROGRESS' })),
        },
      })
      await queueSettled()

      await user.click(screen.getByRole('button', { name: 'Change the status of WO-0001' }))
      await screen.findByRole('menu')
      expect(offeredActions()).toEqual(['Start operation', 'Cancel operation'])

      await user.click(screen.getByRole('menuitem', { name: 'Start operation' }))
      await user.click(await screen.findByRole('button', { name: 'Start operation' }))

      await waitFor(() => fetch.requestFor('POST', '/production/work-orders/wo-1/status'))
      // `occurred_at: null` is what tells the API to stamp the moment the
      // request lands; `""` would be a 422 on a datetime field, and omitting the
      // key altogether would compile just as happily.
      expect(fetch.requestFor('POST', '/production/work-orders/wo-1/status')).toEqual({
        status: 'IN_PROGRESS',
        reason: null,
        occurred_at: null,
      })
    },
    POPPER,
  )

  it(
    'offers Complete and Cancel from IN_PROGRESS, and will not cancel unexplained',
    async () => {
      // Reason is optional on the wire, and when it is omitted the audit row
      // records the literal target status as its own reason - which tells the
      // next reader nothing at all about an abandoned operation.
      const { user, fetch } = renderScreen(<WorkOrdersPage />, {
        permissions: [P.WORK_ORDER_VIEW, P.WORK_ORDER_MANAGE],
        routes: queue([
          workOrder({ status: 'IN_PROGRESS', actual_start_at: '2026-02-02T06:05:00Z' }),
        ]),
      })
      await queueSettled()

      await user.click(screen.getByRole('button', { name: 'Change the status of WO-0001' }))
      await screen.findByRole('menu')
      // No second start, and no edge back to PENDING.
      expect(offeredActions()).toEqual(['Complete operation', 'Cancel operation'])

      await user.click(screen.getByRole('menuitem', { name: 'Cancel operation' }))
      await user.click(await screen.findByRole('button', { name: 'Cancel operation' }))

      expect(await screen.findByText('Record why this operation is being cancelled')).toBeVisible()
      expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
    },
    POPPER,
  )

  it('offers nothing at all on a COMPLETED operation, and says it is closed', async () => {
    // `update_work_order` refuses a terminal operation before it even looks at
    // the payload, so an Assign button here is a form filled in for a 422.
    renderScreen(<WorkOrdersPage />, {
      permissions: [P.WORK_ORDER_VIEW, P.WORK_ORDER_MANAGE],
      routes: queue([
        workOrder({
          status: 'COMPLETED',
          completed_quantity: '100.0000',
          actual_start_at: '2026-02-02T06:05:00Z',
          actual_end_at: '2026-02-02T07:40:00Z',
        }),
      ]),
    })
    await queueSettled()

    expect(screen.getByText('Closed')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Assign WO-0001' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Change the status of WO-0001' })).toBeNull()
  })

  it('offers nothing on a CANCELLED operation either', async () => {
    renderScreen(<WorkOrdersPage />, {
      permissions: [P.WORK_ORDER_VIEW, P.WORK_ORDER_MANAGE],
      routes: queue([workOrder({ status: 'CANCELLED' })]),
    })
    await queueSettled()

    expect(screen.getByText('Closed')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Assign WO-0001' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Change the status of WO-0001' })).toBeNull()
  })
})

// ------------------------------------------------------------- the query ---

describe('what the queue asks the API for', () => {
  it('spends a job id on the path and never sends it twice', async () => {
    // `GET /production/{id}/work-orders` overwrites any `production_order_id` in
    // the query string with the path segment. Sending both is how a filter comes
    // to look applied while the endpoint has already ignored it.
    const seen: { url?: URL } = {}
    renderScreen(<WorkOrdersPage />, {
      permissions: [P.WORK_ORDER_VIEW],
      route: '/work-orders?production_order_id=po-1',
      routes: {
        ...LOOKUPS,
        'GET /production/po-1/work-orders': ({ url }) => {
          seen.url = url
          return page([workOrder()])
        },
      },
    })
    await queueSettled()

    expect(seen.url!.searchParams.get('production_order_id')).toBeNull()
    // Within one job the traveller order is the sequence, not the clock.
    expect(seen.url!.searchParams.get('sort_by')).toBe('sequence')
  })

  it('reads the whole floor in planned-start order', async () => {
    const seen: { url?: URL } = {}
    renderScreen(<WorkOrdersPage />, {
      permissions: [P.WORK_ORDER_VIEW],
      routes: {
        ...LOOKUPS,
        'GET /production/work-orders': ({ url }) => {
          seen.url = url
          return page([workOrder()])
        },
      },
    })
    await queueSettled()

    // Only four fields sort, and `sort_by` is validated rather than ignored, so
    // a wrong default empties the table with a 422 instead of degrading.
    expect(seen.url!.searchParams.get('sort_by')).toBe('planned_start_at')
  })

  it('offers a machine filter only once a work centre narrows it', async () => {
    // A machine belongs to exactly one cell and the API refuses one from any
    // other, so an unscoped machine list would offer choices that 422.
    const unscoped = renderScreen(<WorkOrdersPage />, {
      permissions: [P.WORK_ORDER_VIEW],
      routes: queue([workOrder()]),
    })
    await queueSettled()
    expect(screen.queryByRole('combobox', { name: 'Machine' })).toBeNull()
    unscoped.unmount()

    renderScreen(<WorkOrdersPage />, {
      permissions: [P.WORK_ORDER_VIEW],
      route: '/work-orders?work_center_id=wc-1',
      routes: {
        ...queue([workOrder()]),
        'GET /routing/machines': page([{ id: 'm-1', code: 'CNC-3', name: 'Haas VF2' }]),
      },
    })
    expect(await screen.findByRole('combobox', { name: 'Machine' })).toBeVisible()
  })

  it('explains an empty queue rather than leaving a blank table', async () => {
    renderScreen(<WorkOrdersPage />, {
      permissions: [P.WORK_ORDER_VIEW],
      routes: queue([]),
    })

    expect(await screen.findByText('No operations here')).toBeVisible()
    // Where operations come from is the actionable half of an empty queue.
    expect(screen.getByText(/a production order is released/i)).toBeVisible()
  })
})

// ------------------------------------------------------------ assignment ---

describe('assigning an operation', () => {
  it('sends only the field that changed, so an unreadable lookup cannot blank one', async () => {
    // `WorkOrderUpdate` is applied with `exclude_unset`: an omitted key leaves
    // the column alone. The operator and machine lists are permissioned
    // separately from the queue and soft-fail to empty, so posting every field
    // would let an account that cannot read them unassign whoever is on the job.
    const { user, fetch } = renderScreen(<WorkOrdersPage />, {
      permissions: [P.WORK_ORDER_VIEW, P.WORK_ORDER_MANAGE],
      routes: {
        ...queue([workOrder({ shift: 'A' })]),
        'PATCH /production/work-orders/wo-1': ok(workOrder({ shift: 'B' })),
      },
    })
    await queueSettled()

    await user.click(screen.getByRole('button', { name: 'Assign WO-0001' }))
    await screen.findByLabelText('Shift')
    await user.clear(screen.getByLabelText('Shift'))
    await user.type(screen.getByLabelText('Shift'), 'B')
    await user.click(screen.getByRole('button', { name: 'Save assignment' }))

    await waitFor(() => fetch.requestFor('PATCH', '/production/work-orders/wo-1'))
    // Exactly one key. An `operator_user_id: null` in here is the defect.
    expect(fetch.requestFor('PATCH', '/production/work-orders/wo-1')).toEqual({ shift: 'B' })
  })

  it('does not send an empty PATCH when nothing was touched', async () => {
    // An empty PATCH is accepted and does nothing, so it would end in a success
    // toast for a change that was never made.
    const { user, fetch } = renderScreen(<WorkOrdersPage />, {
      permissions: [P.WORK_ORDER_VIEW, P.WORK_ORDER_MANAGE],
      routes: queue([workOrder({ shift: 'A' })]),
    })
    await queueSettled()

    await user.click(screen.getByRole('button', { name: 'Assign WO-0001' }))
    await screen.findByLabelText('Shift')
    await user.click(screen.getByRole('button', { name: 'Save assignment' }))

    expect(fetch.calls.filter((call) => call.method === 'PATCH')).toHaveLength(0)
  })
})
