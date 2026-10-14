/**
 * Job work: orders sent out for processing.
 *
 * The row menu on this screen is where four separate permissions meet, and the
 * mapping is not the obvious one. `apps/api/src/modules/job_work/router.py` is
 * the authority:
 *
 *     POST   /job-work/{id}/approve   job_work.close
 *     POST   /job-work/{id}/close     job_work.close
 *     POST   /job-work                job_work.create
 *     PATCH  /job-work/{id}           job_work.create
 *     DELETE /job-work/{id}           job_work.create
 *     POST   /job-work/{id}/cancel    job_work.create
 *     POST   /job-work/{id}/issue     job_work.issue
 *     POST   /job-work/{id}/receive   job_work.receive
 *
 * So **approving is the senior `job_work.close` right, not the issue or receive
 * right**, and **raising, amending, cancelling and deleting are all one right**
 * - the API treats raising a document and un-raising it as the same duty. An
 * earlier version of this screen substituted issue/receive for approve and
 * close, which both showed the action to people the API would refuse and hid it
 * from the people who held the right. Every gate below is therefore granted one
 * right at a time.
 *
 * The lifecycle is asserted alongside it, because the two interact: a right is
 * only half the answer to whether an action is offered. Cancellation stops at
 * APPROVED - past that point the order is the only record of where company stock
 * went, and a consignment that never comes back is written off by a final
 * receipt rather than implied by a cancelled document.
 *
 * On the long timeouts: mounting a Radix *Popper* overlay - the row menu is one
 * - costs about twenty seconds of CPU under jsdom every time it opens, before
 * any assertion runs. The menu is therefore opened three times in this file, and
 * each of those tests carries the whole action through to the request it sends.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen } from '@/test/harness'

import type { JobWorkOrderRow, JobWorkStatus } from './job-work-model'
import JobWorkPage from './job-work-page'

/** One Radix Popper mount, plus room for the rest of the test. */
const POPPER = 90_000

// ------------------------------------------------------------- fixtures ----

function jobWorkOrder(overrides: Partial<JobWorkOrderRow> = {}): JobWorkOrderRow {
  return {
    id: 'jw-1',
    code: 'JW-0001',
    status: 'DRAFT',
    version: 1,
    vendor_id: 'vendor-aaaa-0001',
    warehouse_id: 'wh-1',
    return_warehouse_id: 'wh-1',
    cost_centre_id: null,
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
    expected_return_date: '2026-04-10',
    issued_at: null,
    approved_at: null,
    approved_by_id: null,
    closed_at: null,
    cancellation_reason: null,
    source_type: 'MANUAL',
    source_id: null,
    notes: null,
    ...overrides,
  }
}

const listed = (rows: JobWorkOrderRow[]) => ({ 'GET /job-work': page(rows) })

/** Render the orders tab with one order in the given state. */
function renderOrders(status: JobWorkStatus, permissions: string[]) {
  return renderScreen(<JobWorkPage />, {
    permissions: [P.JOB_WORK_VIEW, ...permissions],
    routes: listed([jobWorkOrder({ status })]),
  })
}

/** The labels on the open row menu, in the order the screen offers them. */
function offeredActions(): string[] {
  return within(screen.getByRole('menu'))
    .getAllByRole('menuitem')
    .map((item) => item.textContent?.trim() ?? '')
}

// ---------------------------------------------------------- authorisation --

describe('who may do what to an order', () => {
  it('offers no action at all to a plain viewer', async () => {
    renderOrders('DRAFT', [])

    expect(await screen.findByText('JW-0001')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Actions for JW-0001' })).toBeNull()
    expect(screen.queryByRole('button', { name: /new job work order/i })).toBeNull()
  })

  it('gates raising an order on job_work.create, not on the senior close right', async () => {
    // `POST /job-work` declares `job_work.create`. Gating the button on
    // `job_work.close` would offer it to approvers who cannot raise anything and
    // hide it from the planners who do the raising.
    const closer = renderOrders('DRAFT', [P.JOB_WORK_CLOSE])
    expect(await screen.findByText('JW-0001')).toBeVisible()
    expect(screen.queryByRole('button', { name: /new job work order/i })).toBeNull()
    closer.unmount()

    renderOrders('DRAFT', [P.JOB_WORK_CREATE])
    expect(await screen.findByRole('button', { name: /new job work order/i })).toBeVisible()
  })

  it(
    'offers a draft-holder amend, cancel and delete - and never approval',
    async () => {
      // All four of these are `job_work.create`. Approval is not, and offering it
      // here would put a planner through a confirmation the API refuses.
      const seen: { url?: URL } = {}
      const { user, fetch } = renderScreen(<JobWorkPage />, {
        permissions: [P.JOB_WORK_VIEW, P.JOB_WORK_CREATE],
        routes: {
          ...listed([jobWorkOrder({ status: 'DRAFT' })]),
          'DELETE /job-work/jw-1': ({ url }) => {
            seen.url = url
            return ok({ deleted: true })
          },
        },
      })
      await screen.findByText('JW-0001')

      await user.click(screen.getByRole('button', { name: 'Actions for JW-0001' }))
      await screen.findByRole('menu')
      expect(offeredActions()).toEqual(['Amend draft', 'Cancel order', 'Delete draft'])

      await user.click(screen.getByRole('menuitem', { name: 'Delete draft' }))
      await screen.findByRole('dialog')

      // The API requires the audit reason and refuses without it, so the floor
      // is held here rather than coming back as a 422 on a confirmation the user
      // has already given.
      await user.click(screen.getByRole('button', { name: 'Delete draft' }))
      expect(await screen.findByText(/At least 3 characters/)).toBeVisible()
      expect(fetch.calls.filter((call) => call.method === 'DELETE')).toHaveLength(0)

      await user.type(screen.getByRole('textbox', { name: 'Reason' }), '  Raised in error  ')
      await user.click(screen.getByRole('button', { name: 'Delete draft' }))

      await waitFor(() => fetch.requestFor('DELETE', '/job-work/jw-1'))
      // This is the one endpoint in the module that takes its reason as a query
      // parameter: a DELETE body is legal but several clients and proxies drop
      // it, so sending it in a body here would be a 422 at the moment of use.
      expect(seen.url!.searchParams.get('reason')).toBe('Raised in error')
      expect(fetch.requestFor('DELETE', '/job-work/jw-1')).toBeUndefined()
    },
    POPPER,
  )

  it(
    'gates approval on job_work.close, and that right offers nothing else on a draft',
    async () => {
      const { user, fetch } = renderScreen(<JobWorkPage />, {
        permissions: [P.JOB_WORK_VIEW, P.JOB_WORK_CLOSE],
        routes: {
          ...listed([jobWorkOrder({ status: 'DRAFT' })]),
          'POST /job-work/jw-1/approve': ok(jobWorkOrder({ status: 'APPROVED' })),
        },
      })
      await screen.findByText('JW-0001')

      await user.click(screen.getByRole('button', { name: 'Actions for JW-0001' }))
      await screen.findByRole('menu')
      // Approval only: amending, cancelling and deleting are `job_work.create`.
      expect(offeredActions()).toEqual(['Approve'])

      await user.click(screen.getByRole('menuitem', { name: 'Approve' }))
      await screen.findByRole('dialog')
      await user.type(screen.getByRole('textbox', { name: 'Reason' }), '  Rate agreed  ')
      await user.click(screen.getByRole('button', { name: 'Approve order' }))

      await waitFor(() => fetch.requestFor('POST', '/job-work/jw-1/approve'))
      // Trimmed, and in the body - approve and close are not the delete.
      expect(fetch.requestFor('POST', '/job-work/jw-1/approve')).toEqual({
        reason: 'Rate agreed',
      })
    },
    POPPER,
  )
})

// -------------------------------------------------------------- lifecycle --

describe('what the lifecycle allows', () => {
  it(
    'offers issue and receive while the material is out, and no close and no cancel',
    async () => {
      // The state machine has no edge out of ISSUED except through a receipt:
      // past that point the order is the only record of where company stock
      // went. Closing is legal only from RECEIVED onwards, and a second challan
      // against an already-issued order is perfectly normal.
      const { user } = renderScreen(<JobWorkPage />, {
        permissions: [
          P.JOB_WORK_VIEW,
          P.JOB_WORK_CREATE,
          P.JOB_WORK_ISSUE,
          P.JOB_WORK_RECEIVE,
          P.JOB_WORK_CLOSE,
        ],
        routes: listed([jobWorkOrder({ status: 'ISSUED', issued_at: '2026-04-01T08:00:00Z' })]),
      })
      await screen.findByText('JW-0001')

      await user.click(screen.getByRole('button', { name: 'Actions for JW-0001' }))
      await screen.findByRole('menu')
      // Every right in the module is held here, so anything missing is the
      // lifecycle talking and nothing else.
      expect(offeredActions()).toEqual(['Issue to vendor', 'Receive output'])
    },
    POPPER,
  )

  it('offers nothing once the material is back, to the rights that moved it', async () => {
    // `acceptsIssue` and `acceptsReceipt` are both false at RECEIVED. A
    // storekeeper holding only those two rights has finished with this order.
    renderOrders('RECEIVED', [P.JOB_WORK_ISSUE, P.JOB_WORK_RECEIVE])

    expect(await screen.findByText('JW-0001')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Actions for JW-0001' })).toBeNull()
  })

  it('does open the menu again at RECEIVED for the right that can close it', async () => {
    renderOrders('RECEIVED', [P.JOB_WORK_CLOSE])

    expect(await screen.findByRole('button', { name: 'Actions for JW-0001' })).toBeVisible()
  })

  it('offers nothing on a CLOSED order, to anybody at all', async () => {
    renderOrders('CLOSED', [
      P.JOB_WORK_CREATE,
      P.JOB_WORK_ISSUE,
      P.JOB_WORK_RECEIVE,
      P.JOB_WORK_CLOSE,
    ])

    expect(await screen.findByText('JW-0001')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Actions for JW-0001' })).toBeNull()
  })
})

// ------------------------------------------------------------- the queries --

describe('what the two tabs ask the API for', () => {
  it('narrows the orders list on at_vendor_only, which is the filter it supports', async () => {
    // `GET /job-work` takes `at_vendor_only` and not `overdue_only`; the latter
    // was dropped in silence, so the toggle looked applied and changed nothing.
    const seen: { url?: URL } = {}
    renderScreen(<JobWorkPage />, {
      permissions: [P.JOB_WORK_VIEW],
      route: '/job-work?overdue_only=true&status=ISSUED',
      routes: {
        'GET /job-work': ({ url }) => {
          seen.url = url
          return page([jobWorkOrder({ status: 'ISSUED' })])
        },
      },
    })
    await screen.findByText('JW-0001')

    expect(seen.url!.searchParams.get('at_vendor_only')).toBe('true')
    expect(seen.url!.searchParams.get('overdue_only')).toBeNull()
    expect(seen.url!.searchParams.get('status')).toBe('ISSUED')
  })

  it('sends overdue_only on the pending-at-vendor tab, where it is supported', async () => {
    const seen: { url?: URL } = {}
    renderScreen(<JobWorkPage />, {
      permissions: [P.JOB_WORK_VIEW],
      route: '/job-work?tab=pending&overdue_only=true',
      routes: {
        'GET /job-work/pending-at-vendor': ({ url }) => {
          seen.url = url
          return page([
            {
              vendor_id: 'vendor-aaaa-0001',
              item_id: 'item-bbbb-0001',
              warehouse_id: 'wh-1',
              order_count: 2,
              outstanding_quantity: '140.0000',
              oldest_issued_at: '2026-03-02',
              earliest_expected_return: '2026-03-20',
              ageing_days: 41,
              is_overdue: true,
            },
          ])
        },
      },
    })

    expect(await screen.findByText('Overdue')).toBeVisible()
    expect(seen.url!.searchParams.get('overdue_only')).toBe('true')
    // This stock is still the company's, and saying so is the point of the tab.
    expect(screen.getByText('This stock is still yours')).toBeVisible()
  })

  it('names the vendor from the shared options list rather than printing its id', async () => {
    // `JobWorkOrderSummary` carries `vendor_id` and no name. The column used to
    // show the first eight characters of the uuid, which nobody can act on;
    // the bounded vendor options list turns it into the name and code.
    renderScreen(<JobWorkPage />, {
      permissions: [P.JOB_WORK_VIEW, P.VENDOR_VIEW],
      routes: {
        ...listed([jobWorkOrder({ status: 'APPROVED' })]),
        'GET /vendors/options': ok([
          {
            id: 'vendor-aaaa-0001',
            code: 'VEN-001',
            name: 'Rolled Metals',
            vendor_types: ['JOB_WORK'],
            is_approved: true,
            rating: null,
            lead_time_days: 7,
          },
        ]),
      },
    })

    expect(await screen.findByText('Rolled Metals')).toBeVisible()
    expect(screen.getByText('VEN-001')).toBeVisible()
    expect(screen.queryByText('vendor-a')).toBeNull()
  })

  it('says an empty register is empty, and a refused one refused', async () => {
    const empty = renderScreen(<JobWorkPage />, {
      permissions: [P.JOB_WORK_VIEW],
      routes: { 'GET /job-work': page([]) },
    })
    expect(await screen.findByText('No job work orders found')).toBeVisible()
    empty.unmount()

    renderScreen(<JobWorkPage />, {
      permissions: [P.JOB_WORK_VIEW],
      routes: {
        'GET /job-work': fail(403, 'PERMISSION_DENIED', 'Job work is not yours to see'),
      },
    })
    expect(await screen.findByText('Could not load this list')).toBeVisible()
    expect(screen.getByText('Job work is not yours to see')).toBeVisible()
  })
})
