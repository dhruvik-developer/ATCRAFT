/**
 * Packing orders, and the carton builder behind them.
 *
 * The assertion this file exists for is the hand-over gate. Every other action
 * on a packing order is `packing.manage`, but `POST /packing/{id}/hand-over`
 * guards with `dispatch.create` - handing packed boxes to a consignment is a
 * dispatch act, not a packing one. Gating the button on `packing.manage` with
 * everything else shipped, and it showed packers a button the API refuses: the
 * order looked handed over on the bench and was still sitting there in the
 * system. A test that stubbed `can()` to true would have passed throughout.
 *
 * The rest of the file follows the state machine. `CARTON_EDITABLE_STATUSES` is
 * exactly `(IN_PROGRESS,)` and each transition has exactly one legal
 * predecessor, so an action offered from the wrong state puts a supervisor
 * through a confirmation and a recorded reason for a 409.
 */

import { act, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type RouteRequest } from '@/test/harness'

import PackingPage from './packing-page'
import type { PackingOrderRow, PackingStatus } from './packing-types'

const ORDER_ID = '01a00000-0000-7000-8000-0000000000k1'
const CUSTOMER_ID = '01a00000-0000-7000-8000-0000000000c1'
const WAREHOUSE_ID = '01a00000-0000-7000-8000-0000000000w1'

/** One row of `GET /packing` (`PackingOrderSummary`). */
function order(overrides: Partial<PackingOrderRow> = {}): PackingOrderRow {
  return {
    id: ORDER_ID,
    code: 'PK-0001',
    sales_order_id: null,
    customer_id: CUSTOMER_ID,
    warehouse_id: WAREHOUSE_ID,
    status: 'DRAFT',
    planned_date: '2026-09-05',
    packed_date: null,
    packed_by_id: null,
    total_cartons: 0,
    total_gross_weight: '0.000',
    total_net_weight: '0.000',
    dispatch_id: null,
    handed_over_at: null,
    notes: null,
    version: 1,
    ...overrides,
  }
}

/** `GET /packing/{id}` - the header plus its lines. Cartons are separate. */
function orderDetail(status: PackingStatus, overrides: Partial<PackingOrderRow> = {}) {
  return {
    ...order({ status, ...overrides }),
    lines: [
      {
        id: '01a00000-0000-7000-8000-0000000000l1',
        packing_order_id: ORDER_ID,
        line_number: 1,
        sales_order_line_id: null,
        item_id: '01a00000-0000-7000-8000-0000000000i1',
        quantity_to_pack: '10.0000',
        packed_quantity: '0.0000',
        uom_id: '01a00000-0000-7000-8000-0000000000u1',
      },
    ],
  }
}

/** Records the query string of each request, which `fetch.calls` drops. */
function urlRecorder() {
  const urls: URL[] = []
  return {
    urls,
    paramsAt(index: number): Record<string, string> {
      const url = urls[index]
      expect(url, `expected at least ${index + 1} request(s)`).toBeDefined()
      return Object.fromEntries(url!.searchParams)
    },
  }
}

/**
 * Open the carton builder for an order in a given state.
 *
 * The routes an open order needs depend on its state: only an IN_PROGRESS order
 * shows the add-carton form, and only that form reads the packing materials.
 */
async function openOrder(
  status: PackingStatus,
  permissions: string[],
  routes: Record<string, unknown> = {},
) {
  const rendered = renderScreen(<PackingPage />, {
    permissions,
    routes: {
      'GET /packing': page([order({ status })]),
      'GET /packing/packing-materials': page([]),
      [`GET /packing/${ORDER_ID}`]: ok(orderDetail(status)),
      [`GET /packing/${ORDER_ID}/cartons`]: page([]),
      ...routes,
    },
  })
  await rendered.user.click(await screen.findByRole('button', { name: /^pack$|^view$/i }))
  // The dialog is titled with the order, and waiting on that means the detail
  // query has landed before anything is asserted.
  await screen.findByText('Packing PK-0001')
  return rendered
}

describe('the packing list', () => {
  it('says whether the button will pack or only show', async () => {
    // The same row action does two different things, so it must not claim it
    // will pack for somebody whose account can only read the order.
    renderScreen(<PackingPage />, {
      permissions: [P.PACKING_VIEW, P.PACKING_MANAGE],
      routes: { 'GET /packing': page([order()]) },
    })
    expect(await screen.findByRole('button', { name: 'Pack' })).toBeVisible()

    renderScreen(<PackingPage />, {
      permissions: [P.PACKING_VIEW],
      routes: { 'GET /packing': page([order()]) },
    })
    expect(await screen.findByRole('button', { name: 'View' })).toBeVisible()
  })

  it('calls out a packed order that nothing has collected yet', async () => {
    // This is the despatch bay's working queue: packed, sealed, and still on
    // the floor because no consignment has been raised to take it.
    renderScreen(<PackingPage />, {
      permissions: [P.PACKING_VIEW],
      routes: {
        'GET /packing': page([order({ status: 'PACKED', packed_date: '2026-09-04T10:00:00Z' })]),
      },
    })

    const row = (await screen.findByText('PK-0001')).closest('tr')
    expect(row).not.toBeNull()
    expect(within(row!).getByText('Awaiting dispatch')).toBeVisible()
  })

  it('forwards each filter under the name the endpoint answers', async () => {
    const recorder = urlRecorder()
    renderScreen(<PackingPage />, {
      permissions: [P.PACKING_VIEW],
      route:
        '/packing?status=PACKED&open_only=true&awaiting_dispatch=true&planned_from=2026-09-01&planned_to=2026-09-30',
      routes: {
        'GET /packing': ({ url }) => {
          recorder.urls.push(url)
          return page([order({ status: 'PACKED' })])
        },
      },
    })

    expect(await screen.findByText('PK-0001')).toBeVisible()
    const params = recorder.paramsAt(0)
    expect(params.status).toBe('PACKED')
    // The two flags are separate questions - "still on the bench" and "packed
    // but not collected" - and a URL that carries both must send both.
    expect(params.open_only).toBe('true')
    expect(params.awaiting_dispatch).toBe('true')
    expect(params.planned_from).toBe('2026-09-01')
    expect(params.planned_to).toBe('2026-09-30')
    // Oldest planned first: the order due today is the one being worked.
    expect(params.sort_by).toBe('planned_date')
    expect(params.sort_dir).toBe('asc')
  })

  it('explains an empty list rather than showing a bare table', async () => {
    renderScreen(<PackingPage />, {
      permissions: [P.PACKING_VIEW],
      routes: { 'GET /packing': page([]) },
    })

    expect(await screen.findByText('No packing orders')).toBeVisible()
  })
})

describe('the hand-over gate', () => {
  it('offers the hand-over to a packer who may also raise a dispatch', async () => {
    await openOrder('PACKED', [P.PACKING_VIEW, P.PACKING_MANAGE, P.DISPATCH_CREATE])

    expect(screen.getByRole('button', { name: /hand to dispatch/i })).toBeVisible()
  })

  it('withholds it from a packer who may not - this is the defect that shipped', async () => {
    // `packing.manage` is enough to open a box, seal it and complete the order.
    // It is not enough to load those boxes onto a consignment: the API guards
    // `hand-over` with `dispatch.create`. Gating the button with everything
    // else showed packers an action the API refuses.
    await openOrder('PACKED', [P.PACKING_VIEW, P.PACKING_MANAGE])

    expect(screen.queryByRole('button', { name: /hand to dispatch/i })).toBeNull()
    // Hand-over is the only transition PACKED allows, so the panel is empty -
    // but the order itself is still open to a packer, which is what tells this
    // apart from `packing.manage` having been withheld altogether.
    expect(screen.queryByRole('button', { name: /^complete$/i })).toBeNull()
    expect(screen.getByRole('button', { name: /generate packing list/i })).toBeInTheDocument()
  })

  it('will not hand over until a consignment is named', async () => {
    const { user, fetch } = await openOrder(
      'PACKED',
      [P.PACKING_VIEW, P.PACKING_MANAGE, P.DISPATCH_CREATE],
      { 'GET /dispatch': page([]) },
    )

    await user.click(screen.getByRole('button', { name: /hand to dispatch/i }))
    await user.type(await screen.findByLabelText(/reason/i), 'Loaded onto the 4pm run')
    await user.click(screen.getByRole('button', { name: /^hand over$/i }))

    // The boxes have to be loaded onto *something*. Posting without the
    // consignment id would be a 422 on a field the operator never filled in,
    // so the confirmation simply does not fire.
    await waitFor(() => expect(screen.getByRole('button', { name: /^hand over$/i })).toBeVisible())
    expect(fetch.calls.some((call) => call.path.endsWith('/hand-over'))).toBe(false)
  })

  it('says where to go when the customer has no consignment waiting', async () => {
    // An empty picker with no explanation reads as a broken screen. The answer
    // is on another screen entirely, so the message says so.
    const { user } = await openOrder(
      'PACKED',
      [P.PACKING_VIEW, P.PACKING_MANAGE, P.DISPATCH_CREATE],
      { 'GET /dispatch': page([]) },
    )

    await user.click(screen.getByRole('button', { name: /hand to dispatch/i }))
    expect(await screen.findByText(/no consignment waiting to be loaded/i)).toBeVisible()
  })

  it('asks only for this customer’s consignments that have not left', async () => {
    const recorder = urlRecorder()
    const { user } = await openOrder(
      'PACKED',
      [P.PACKING_VIEW, P.PACKING_MANAGE, P.DISPATCH_CREATE],
      {
        'GET /dispatch': ({ url }: RouteRequest) => {
          recorder.urls.push(url)
          return page([])
        },
      },
    )

    await user.click(screen.getByRole('button', { name: /hand to dispatch/i }))
    await waitFor(() => expect(recorder.urls.length).toBe(1))

    // Loading boxes onto another customer's lorry, or onto one that has already
    // gone, are both things the picker must not make possible.
    const params = recorder.paramsAt(0)
    expect(params.customer_id).toBe(CUSTOMER_ID)
    expect(params.status).toBe('NOT_DISPATCHED')
  })
})

describe('the order lifecycle', () => {
  it('offers only the start from a draft', async () => {
    await openOrder('DRAFT', [P.PACKING_VIEW, P.PACKING_MANAGE])

    expect(screen.getByRole('button', { name: /start packing/i })).toBeVisible()
    expect(screen.queryByRole('button', { name: /^complete$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /hand to dispatch/i })).toBeNull()
    // Three controls in this dialog can answer to "Close": the header's X, the
    // footer's dismiss, and - only once the order is handed over - the
    // lifecycle action that ends it for good. They share one accessible name,
    // which is reported as an accessibility defect. On a draft only the two
    // pieces of dialog chrome exist, so a third would be an action offered from
    // a state the API refuses.
    expect(screen.getAllByRole('button', { name: /^close$/i })).toHaveLength(2)
    // A draft has no bench: cartons may only be opened once it is in progress,
    // and saying so is what stops a packer looking for a missing button.
    expect(screen.getByText('Packing has not started')).toBeVisible()
  })

  it('offers the completion and the cancellation while it is in progress', async () => {
    await openOrder('IN_PROGRESS', [P.PACKING_VIEW, P.PACKING_MANAGE])

    expect(screen.getByRole('button', { name: /^complete$/i })).toBeVisible()
    expect(screen.getByRole('button', { name: /cancel order/i })).toBeVisible()
    expect(screen.queryByRole('button', { name: /start packing/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /hand to dispatch/i })).toBeNull()
  })

  it('offers only the close once the boxes have gone', async () => {
    // Nothing is cancellable after hand-over: the material has left the bench,
    // and the abandonment path would put stock back that is no longer there.
    const { user } = await openOrder('HANDED_OVER', [
      P.PACKING_VIEW,
      P.PACKING_MANAGE,
      P.DISPATCH_CREATE,
    ])

    // The lifecycle action sits in the body, above the two pieces of dialog
    // chrome that share its name; there is nothing but document order to tell
    // them apart, which is the accessibility defect this reports.
    const closes = screen.getAllByRole('button', { name: /^close$/i })
    expect(closes).toHaveLength(3)
    await user.click(closes[0]!)
    // Proof that the extra one really is the transition rather than more
    // chrome: it asks for the audited reason before it closes the order.
    expect(await screen.findByText('Close PK-0001?')).toBeVisible()

    expect(screen.queryByRole('button', { name: /hand to dispatch/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /cancel order/i })).toBeNull()
  })

  it('gives a reader no lifecycle actions at all', async () => {
    await openOrder('DRAFT', [P.PACKING_VIEW])

    expect(screen.queryByRole('button', { name: /start packing/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /cancel order/i })).toBeNull()
  })
})

describe('what a transition sends', () => {
  it('records the reason the API insists on, and refuses to send without one', async () => {
    const { user, fetch } = await openOrder('DRAFT', [P.PACKING_VIEW, P.PACKING_MANAGE], {
      [`POST /packing/${ORDER_ID}/start`]: ok(orderDetail('IN_PROGRESS')),
    })

    await user.click(screen.getByRole('button', { name: /start packing/i }))
    await user.click(await screen.findByRole('button', { name: /^start packing$/i, hidden: false }))

    // Every transition on this order is audited, and the API refuses a bodyless
    // one - so an empty reason is caught here rather than becoming a 422.
    expect(await screen.findByText(/it needs a reason/i)).toBeVisible()
    expect(fetch.calls.some((call) => call.path.endsWith('/start'))).toBe(false)

    // `Reason` is bounded 3..255 on the API. A two-character reason is a
    // pydantic 422 keyed on `reason`, which the query client suppresses on the
    // assumption the form shows it - so the form has to, or the order looks
    // started on the bench and is still a draft in the system.
    await user.type(screen.getByLabelText(/reason/i), 'ok')
    await user.click(screen.getAllByRole('button', { name: /^start packing$/i }).at(-1)!)
    expect(await screen.findByText(/between 3 and 255 characters/i)).toBeVisible()
    expect(fetch.calls.some((call) => call.path.endsWith('/start'))).toBe(false)

    await user.clear(screen.getByLabelText(/reason/i))
    await user.type(screen.getByLabelText(/reason/i), 'Bench free, starting now')
    await user.click(screen.getAllByRole('button', { name: /^start packing$/i }).at(-1)!)

    const body = (await waitFor(() =>
      fetch.requestFor('POST', `/packing/${ORDER_ID}/start`),
    )) as Record<string, unknown>
    expect(body).toEqual({ reason: 'Bench free, starting now' })
  })

  it('completes a full pack without asking for a short pack', async () => {
    const { user, fetch } = await openOrder('IN_PROGRESS', [P.PACKING_VIEW, P.PACKING_MANAGE], {
      [`POST /packing/${ORDER_ID}/complete`]: ok({
        order: orderDetail('PACKED'),
        total_cartons: 1,
        total_gross_weight: '12.500',
        total_net_weight: '11.000',
        short_packed_line_numbers: [],
      }),
    })

    await user.click(screen.getByRole('button', { name: /^complete$/i }))
    await user.type(await screen.findByLabelText(/reason/i), 'All lines packed')
    await user.click(screen.getByRole('button', { name: /complete packing/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('POST', `/packing/${ORDER_ID}/complete`),
    )) as Record<string, unknown>

    // The flag is sent explicitly rather than omitted: letting a short pack out
    // of the gate is a decision somebody made, and the audit row should say
    // whether they made it.
    expect(body).toEqual({ reason: 'All lines packed', allow_short_pack: false })
  })

  it('sends the short-pack allowance only when it was actually ticked', async () => {
    const { user, fetch } = await openOrder('IN_PROGRESS', [P.PACKING_VIEW, P.PACKING_MANAGE], {
      [`POST /packing/${ORDER_ID}/complete`]: ok({
        order: orderDetail('PACKED'),
        total_cartons: 1,
        total_gross_weight: '12.500',
        total_net_weight: '11.000',
        short_packed_line_numbers: [1],
      }),
    })

    await user.click(screen.getByRole('button', { name: /^complete$/i }))
    await user.click(await screen.findByLabelText(/allow a short pack/i))
    await user.type(screen.getByLabelText(/reason/i), 'Balance cancelled by the customer')
    await user.click(screen.getByRole('button', { name: /complete packing/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('POST', `/packing/${ORDER_ID}/complete`),
    )) as Record<string, unknown>
    expect(body.allow_short_pack).toBe(true)
  })
})

describe('packing list status recovery', () => {
  const statusPath = '/reports/jobs/packing-job'
  const jobKey = ['reports', 'job', statusPath]
  const accepted = ok({
    job_id: 'packing-job',
    status: 'queued',
    status_url: `/api/v1${statusPath}`,
  })
  const complete = ok({
    id: 'packing-job',
    report_key: 'PACKING_LIST',
    status: 'COMPLETED',
    row_count: 1,
    document_id: 'document',
    download_url: '/api/v1/documents/document/download',
  })

  it.each([404, 403, 500, 'network'] as const)(
    'shows and retries an initial %s status failure without queueing another PDF',
    async (status) => {
      let recovering = false
      const h = await openOrder('PACKED', [P.PACKING_VIEW, P.REPORT_VIEW], {
        [`GET /packing/${ORDER_ID}`]: ok(orderDetail('PACKED', { total_cartons: 1 })),
        [`GET /packing/${ORDER_ID}/packing-list`]: accepted,
        [`GET ${statusPath}`]: () => {
          if (recovering) return complete
          if (status === 'network') throw new Error('Connection lost')
          return fail(status, 'STATUS_FAILED', 'The status could not be read')
        },
      })
      await h.user.click(screen.getByRole('button', { name: /generate packing list/i }))
      expect(await screen.findByText('Packing list could not be generated')).toBeVisible()
      expect(screen.getByRole('button', { name: 'Retry status' })).toBeEnabled()
      expect(screen.queryByRole('button', { name: /generate packing list/i })).toBeNull()

      const polls = h.fetch.calls.filter((call) => call.path === statusPath).length
      await act(async () => {
        vi.useFakeTimers()
        await vi.advanceTimersByTimeAsync(4500)
        vi.useRealTimers()
      })
      expect(h.fetch.calls.filter((call) => call.path === statusPath)).toHaveLength(polls)

      recovering = true
      await h.user.click(screen.getByRole('button', { name: 'Retry status' }))
      expect(await screen.findByText('Packing list is ready')).toBeVisible()
      expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled()
      expect(h.fetch.calls.filter((call) => call.path.endsWith('/packing-list'))).toHaveLength(1)
    },
  )

  it('recovers a failed poll after a QUEUED payload by reading the same job', async () => {
    let state: 'queued' | 'error' | 'complete' = 'queued'
    const h = await openOrder('PACKED', [P.PACKING_VIEW, P.REPORT_VIEW], {
      [`GET /packing/${ORDER_ID}`]: ok(orderDetail('PACKED', { total_cartons: 1 })),
      [`GET /packing/${ORDER_ID}/packing-list`]: accepted,
      [`GET ${statusPath}`]: () =>
        state === 'error'
          ? fail(500, 'STATUS_FAILED', 'Status service unavailable')
          : state === 'complete'
            ? complete
            : ok({ id: 'packing-job', status: 'QUEUED' }),
    })
    await h.user.click(screen.getByRole('button', { name: /generate packing list/i }))
    await waitFor(() => expect(h.queryClient.getQueryState(jobKey)?.status).toBe('success'))
    state = 'error'
    await h.queryClient.invalidateQueries({ queryKey: jobKey })
    expect(await screen.findByText('Status service unavailable')).toBeVisible()
    state = 'complete'
    await h.user.click(screen.getByRole('button', { name: 'Retry status' }))
    expect(await screen.findByText('Packing list is ready')).toBeVisible()
    expect(h.fetch.calls.filter((call) => call.path.endsWith('/packing-list'))).toHaveLength(1)
  })

  it('can dismiss a status error and does not treat a FAILED build as a retryable poll', async () => {
    const h = await openOrder('PACKED', [P.PACKING_VIEW, P.REPORT_VIEW], {
      [`GET /packing/${ORDER_ID}`]: ok(orderDetail('PACKED', { total_cartons: 1 })),
      [`GET /packing/${ORDER_ID}/packing-list`]: accepted,
      [`GET ${statusPath}`]: ok({
        id: 'packing-job',
        status: 'FAILED',
        error_message: 'No carton contents',
      }),
    })
    await h.user.click(screen.getByRole('button', { name: /generate packing list/i }))
    expect(await screen.findByText('No carton contents')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Retry status' })).toBeNull()
    await h.user.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.getByRole('button', { name: /generate packing list/i })).toBeEnabled()
    expect(h.fetch.calls.filter((call) => call.path.endsWith('/packing-list'))).toHaveLength(1)
  })
})
