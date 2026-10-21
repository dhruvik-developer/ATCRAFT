/**
 * Dispatch, and the tracking board beside it.
 *
 * Three things here are worth a test each, and none of them is visible to the
 * compiler.
 *
 * **The state machine has no shortcut.** `NOT_DISPATCHED -> READY_FOR_DISPATCH
 * -> DISPATCHED`, and nothing jumps the middle step. Offering "Dispatch" on a
 * consignment that has not been staged takes an operator through a confirmation
 * and a recorded reason to reach a 409 - and worse, teaches them that the
 * release sometimes fails for no visible reason.
 *
 * **Confirming a delivery is its own right.** `dispatch.confirm_delivery` is
 * separate from `dispatch.update`, because the person who books a consignment
 * out is not the person who attests that it arrived. A gate on `update` here
 * would let anyone who can edit a dispatch close it as delivered.
 *
 * **The transition payloads are not interchangeable.** Three of them demand a
 * `reason`; the delivery takes a `delivery_note` and no reason at all. Posting
 * the same body to all four earns a 422 on a field the operator never saw.
 *
 * A note on cost: every assertion that reads the row's action menu opens a Radix
 * popper, and floating-ui's positioning takes tens of seconds under jsdom. Those
 * tests carry an explicit timeout and are deliberately few, each one covering
 * both what the menu offers and what the resulting request carries.
 */

import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import DispatchPage from './dispatch-page'
import type {
  DispatchDetail,
  DispatchRow,
  DispatchStatus,
  DispatchTrackingRow,
} from './dispatch-types'

/** Long enough for one popper to position itself under jsdom. */
const POPPER = 90_000

const DISPATCH_ID = '01a00000-0000-7000-8000-0000000000d1'

/** A promised date that is still ahead, whatever day the suite runs. */
function promisedAhead(days: number): string {
  const date = new Date()
  date.setDate(date.getDate() + days)
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${month}-${day}`
}

/** One row of `GET /dispatch` (`DispatchSummary`). */
function dispatch(overrides: Partial<DispatchRow> = {}): DispatchRow {
  return {
    sales_invoice_id: null,
    id: DISPATCH_ID,
    code: 'DSP-0001',
    dispatch_type: 'SALES',
    status: 'NOT_DISPATCHED',
    customer_id: '01a00000-0000-7000-8000-0000000000c1',
    vendor_id: null,
    warehouse_id: '01a00000-0000-7000-8000-0000000000w1',
    sales_order_id: null,
    dispatch_date: '2026-09-02',
    expected_delivery_date: '2026-09-08',
    actual_delivery_date: null,
    transporter_id: null,
    courier_name: 'Blue Dart',
    tracking_id: 'BD-99881',
    total_boxes: 4,
    total_weight_kg: '112.500',
    created_at: '2026-09-01T06:00:00Z',
    ...overrides,
  }
}

function dispatchDetail(overrides: Partial<DispatchDetail> = {}): DispatchDetail {
  return {
    ...dispatch(),
    packing_order_id: null,
    dispatched_at: null,
    transport_name: 'Surface',
    lr_number: 'LR-100',
    vehicle_number: 'GJ01AB1234',
    driver_name: 'A Driver',
    driver_mobile: '9876543210',
    freight_amount: '125.00',
    freight_paid_by: 'CONSIGNOR',
    eway_bill_number: 'EWB-100',
    invoice_document_id: null,
    delivery_confirmed_by: null,
    delivery_note: null,
    delay_reason: null,
    delay_notified_at: null,
    notes: 'Original plan',
    version: 1,
    lines: [
      {
        id: '01a00000-0000-7000-8000-0000000000l1',
        line_number: 1,
        sales_order_line_id: null,
        item_id: '01a00000-0000-7000-8000-0000000000i1',
        quantity: '2.0000',
        batch_id: null,
        serial_ids: [],
        carton_ids: [],
        reservation_id: null,
        inventory_transaction_id: null,
        notes: null,
      },
    ],
    ...overrides,
  }
}

/** One row of `GET /dispatch/tracking` (`TrackingBoardRow`). */
function trackingRow(overrides: Partial<DispatchTrackingRow> = {}): DispatchTrackingRow {
  return {
    dispatch_id: DISPATCH_ID,
    code: 'DSP-0001',
    dispatch_type: 'SALES',
    status: 'IN_TRANSIT',
    party_type: 'CUSTOMER',
    party_name: 'Hardware House',
    reference_code: 'SO-0007',
    courier_name: 'Blue Dart',
    transporter_name: null,
    tracking_id: 'BD-99881',
    dispatch_date: '2026-09-02',
    expected_delivery_date: '2026-09-08',
    actual_delivery_date: null,
    is_delayed: false,
    delay_days: 0,
    delay_reason: null,
    qc_pending: false,
    grn_pending: false,
    ...overrides,
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

/** Render the list and open one row's action menu. */
async function openActions(
  status: DispatchStatus,
  permissions: string[],
  routes: Record<string, unknown> = {},
  row: Partial<DispatchRow> = {},
) {
  const rendered = renderScreen(<DispatchPage />, {
    permissions,
    routes: { 'GET /dispatch': page([dispatch({ status, ...row })]), ...routes },
  })
  await screen.findByText('DSP-0001')
  await rendered.user.click(screen.getByRole('button', { name: 'Actions for DSP-0001' }))
  await screen.findByRole('menuitem', { name: /tracking timeline/i })
  return rendered
}

/** The action names the open menu is offering. */
function menuItems(): string[] {
  return screen.getAllByRole('menuitem').map((item) => item.textContent?.trim() ?? '')
}

describe('who may raise a consignment', () => {
  it('offers the create action to an account holding dispatch.create', async () => {
    renderScreen(<DispatchPage />, {
      permissions: [P.DISPATCH_VIEW, P.DISPATCH_CREATE],
      routes: { 'GET /dispatch': page([dispatch()]) },
    })

    expect(await screen.findByRole('button', { name: /create dispatch/i })).toBeVisible()
  })

  it('withholds it from an account that may only read the board', async () => {
    renderScreen(<DispatchPage />, {
      permissions: [P.DISPATCH_VIEW],
      routes: { 'GET /dispatch': page([dispatch()]) },
    })

    expect(await screen.findByText('DSP-0001')).toBeVisible()
    expect(screen.queryByRole('button', { name: /create dispatch/i })).toBeNull()
  })
})

describe('sales invoice actions', () => {
  it('withholds invoice creation when the account cannot read its required preview', async () => {
    await openActions(
      'READY_FOR_DISPATCH',
      [P.DISPATCH_VIEW, P.SALES_INVOICE_CREATE],
      {},
      { sales_order_id: 'so-1' },
    )
    expect(screen.queryByRole('menuitem', { name: 'Raise invoice' })).toBeNull()
  })

  it('offers invoice creation with both create and preview rights', async () => {
    await openActions(
      'READY_FOR_DISPATCH',
      [P.DISPATCH_VIEW, P.SALES_INVOICE_CREATE, P.SALES_INVOICE_VIEW],
      {},
      { sales_order_id: 'so-1' },
    )
    expect(screen.getByRole('menuitem', { name: 'Raise invoice' })).toBeVisible()
  })
})

describe('pre-dispatch editing', () => {
  it(
    'withholds edits without dispatch.update',
    async () => {
      await openActions('NOT_DISPATCHED', [P.DISPATCH_VIEW])
      expect(screen.queryByRole('menuitem', { name: 'Edit dispatch details' })).toBeNull()
    },
    POPPER,
  )

  it(
    'withholds edits after release',
    async () => {
      await openActions('IN_TRANSIT', [P.DISPATCH_VIEW, P.DISPATCH_UPDATE])
      expect(screen.queryByRole('menuitem', { name: 'Edit dispatch details' })).toBeNull()
    },
    POPPER,
  )

  it(
    'offers edits while the consignment is not dispatched',
    async () => {
      await openActions('READY_FOR_DISPATCH', [P.DISPATCH_VIEW, P.DISPATCH_UPDATE])
      expect(screen.getByRole('menuitem', { name: 'Edit dispatch details' })).toBeVisible()
    },
    POPPER,
  )

  it(
    'loads and saves transport details without replacing the loaded lines',
    async () => {
      const detail = dispatchDetail()
      const { user, fetch } = await openActions(
        'NOT_DISPATCHED',
        [P.DISPATCH_VIEW, P.DISPATCH_UPDATE],
        {
          [`GET /dispatch/${DISPATCH_ID}`]: ok(detail),
          [`PATCH /dispatch/${DISPATCH_ID}`]: ok(detail),
        },
      )

      await user.click(screen.getByRole('menuitem', { name: 'Edit dispatch details' }))
      expect(await screen.findByText('Loaded lines (unchanged)')).toBeVisible()
      expect(screen.getByLabelText('LR number')).toHaveValue('LR-100')

      await user.clear(screen.getByLabelText('LR number'))
      await user.type(screen.getByLabelText('LR number'), 'LR-UPDATED')
      await user.clear(screen.getByLabelText('Expected delivery'))
      await user.click(screen.getByRole('button', { name: 'Save dispatch details' }))

      const body = (await waitFor(() =>
        fetch.requestFor('PATCH', `/dispatch/${DISPATCH_ID}`),
      )) as Record<string, unknown>
      expect(body).toMatchObject({
        dispatch_date: '2026-09-02',
        expected_delivery_date: null,
        lr_number: 'LR-UPDATED',
        freight_amount: '125.00',
      })
      expect(body).not.toHaveProperty('lines')
    },
    POPPER,
  )
})

describe('the two lists', () => {
  it('forwards each list filter under the name the endpoint answers', async () => {
    const recorder = urlRecorder()
    renderScreen(<DispatchPage />, {
      permissions: [P.DISPATCH_VIEW],
      route:
        '/dispatch?status=IN_TRANSIT&dispatch_type=SALES&delayed_only=true&in_flight_only=true',
      routes: {
        'GET /dispatch': ({ url }) => {
          recorder.urls.push(url)
          return page([dispatch({ status: 'IN_TRANSIT' })])
        },
      },
    })

    expect(await screen.findByText('DSP-0001')).toBeVisible()
    const params = recorder.paramsAt(0)
    expect(params.status).toBe('IN_TRANSIT')
    expect(params.dispatch_type).toBe('SALES')
    expect(params.delayed_only).toBe('true')
    expect(params.in_flight_only).toBe('true')
    // Newest first: a dispatch screen is read from today backwards.
    expect(params.sort_by).toBe('dispatch_date')
    expect(params.sort_dir).toBe('desc')
  })

  it('reads the tracking board from its own endpoint, and sorts it not at all', async () => {
    // The board is a different projection, not a filtered dispatch list: it
    // joins both directions of movement and carries the party's name, which the
    // dispatch list does not have. It also takes no sort parameters, so sending
    // one would be silently ignored and the arrow would lie.
    const recorder = urlRecorder()
    const { user, fetch } = renderScreen(<DispatchPage />, {
      permissions: [P.DISPATCH_VIEW],
      routes: {
        'GET /dispatch': page([dispatch()]),
        'GET /dispatch/tracking': ({ url }) => {
          recorder.urls.push(url)
          return page([trackingRow()])
        },
      },
    })

    await screen.findByText('DSP-0001')
    await user.click(screen.getByRole('tab', { name: /tracking board/i }))

    expect(await screen.findByText('Hardware House')).toBeVisible()
    expect(fetch.calls.map((call) => call.path)).toContain('/dispatch/tracking')
    const params = recorder.paramsAt(0)
    expect(params.sort_by).toBeUndefined()
    expect(params.sort_dir).toBeUndefined()
  })

  it('flags a consignment past its promised date', async () => {
    // The whole point of the screen. A row that is late must say so in words -
    // "3 days late" - rather than in a colour somebody has to know to read.
    renderScreen(<DispatchPage />, {
      permissions: [P.DISPATCH_VIEW],
      routes: {
        'GET /dispatch': page([
          dispatch({ status: 'IN_TRANSIT', expected_delivery_date: '2026-08-01' }),
        ]),
      },
    })

    expect(await screen.findByText(/days late/)).toBeVisible()
  })

  it('does not call a settled consignment late', async () => {
    // `delayDays` returns null once the consignment is settled: a delivery that
    // arrived last month is history, not an outstanding delay to chase.
    renderScreen(<DispatchPage />, {
      permissions: [P.DISPATCH_VIEW],
      routes: {
        'GET /dispatch': page([
          dispatch({
            status: 'DELIVERED',
            expected_delivery_date: '2026-08-01',
            actual_delivery_date: '2026-08-03',
          }),
        ]),
      },
    })

    expect(await screen.findByText('DSP-0001')).toBeVisible()
    expect(screen.queryByText(/days late/)).toBeNull()
  })

  it('names a consignment with no tracking reference as untraceable', async () => {
    // A blank cell reads as "nothing to see"; this one means nobody can answer
    // the customer who rings to ask where their order is.
    renderScreen(<DispatchPage />, {
      permissions: [P.DISPATCH_VIEW],
      routes: { 'GET /dispatch': page([dispatch({ tracking_id: null })]) },
    })

    expect(await screen.findByText('Not traceable')).toBeVisible()
  })
})

describe('the lifecycle the row offers', () => {
  it(
    'stages a not-dispatched consignment, and posts the audited reason',
    async () => {
      const { user, fetch } = await openActions(
        'NOT_DISPATCHED',
        [P.DISPATCH_VIEW, P.DISPATCH_UPDATE],
        { [`POST /dispatch/${DISPATCH_ID}/ready`]: ok(null) },
      )

      const items = menuItems()
      expect(items).toContain('Mark ready')
      expect(items).toContain('Cancel dispatch')
      // The rule that makes the state machine worth having: there is no jump
      // from not-dispatched to dispatched. The consignment is staged first.
      expect(items).not.toContain('Dispatch')
      expect(items).not.toContain('Mark in transit')

      await user.click(screen.getByRole('menuitem', { name: 'Mark ready' }))
      await user.click(await screen.findByRole('button', { name: 'Mark ready' }))

      // Every transition is recorded, and the API refuses a bodyless one, so an
      // empty reason is caught here rather than becoming a 422 nobody sees.
      expect(await screen.findByText(/it needs a reason/i)).toBeVisible()
      expect(fetch.calls.some((call) => call.path.endsWith('/ready'))).toBe(false)

      // `Reason` is bounded 3..255 on the API. A two-character reason is a
      // pydantic 422 keyed on `reason`, which the query client suppresses on
      // the assumption the form shows it - so the form has to, or the dialog
      // simply stays open with nothing said.
      await user.type(screen.getByLabelText(/reason/i), 'ok')
      await user.click(screen.getByRole('button', { name: 'Mark ready' }))
      expect(await screen.findByText(/3 to 255 characters/i)).toBeVisible()
      expect(fetch.calls.some((call) => call.path.endsWith('/ready'))).toBe(false)

      await user.clear(screen.getByLabelText(/reason/i))
      await user.type(screen.getByLabelText(/reason/i), 'Staged at the gate')
      await user.click(screen.getByRole('button', { name: 'Mark ready' }))

      const body = (await waitFor(() =>
        fetch.requestFor('POST', `/dispatch/${DISPATCH_ID}/ready`),
      )) as Record<string, unknown>
      expect(body).toEqual({ reason: 'Staged at the gate' })
    },
    POPPER,
  )

  it(
    'releases only a consignment that has been staged',
    async () => {
      await openActions('READY_FOR_DISPATCH', [
        P.DISPATCH_VIEW,
        P.DISPATCH_UPDATE,
        P.DISPATCH_CREATE,
      ])

      const items = menuItems()
      expect(items).toContain('Dispatch')
      // Staging it twice is not a transition the API has.
      expect(items).not.toContain('Mark ready')
      // Still cancellable: nothing has left, so nothing has to come back.
      expect(items).toContain('Cancel dispatch')
    },
    POPPER,
  )

  it(
    'records the actual gate details when a consignment leaves',
    async () => {
      const { user, fetch } = await openActions(
        'READY_FOR_DISPATCH',
        [P.DISPATCH_VIEW, P.DISPATCH_CREATE],
        { [`POST /dispatch/${DISPATCH_ID}/dispatch`]: ok(null) },
      )

      await user.click(screen.getByRole('menuitem', { name: 'Dispatch' }))
      await screen.findByText('Gate details')
      await user.type(screen.getByLabelText(/reason/i), 'Vehicle cleared the gate')
      fireEvent.change(screen.getByLabelText('Actual dispatch date'), {
        target: { value: '2026-10-24' },
      })
      fireEvent.change(screen.getByLabelText('Updated expected delivery'), {
        target: { value: '2026-10-28' },
      })
      await user.type(screen.getByLabelText('LR number'), 'LR-204')
      await user.type(screen.getByLabelText('Vehicle number'), 'MH12AB1234')
      await user.type(screen.getByLabelText('Tracking id'), 'TRACK-204')
      await user.type(screen.getByLabelText('Gate location'), 'Pune plant gate')
      await user.click(screen.getByRole('button', { name: /^Dispatch$/ }))

      const body = (await waitFor(() =>
        fetch.requestFor('POST', `/dispatch/${DISPATCH_ID}/dispatch`),
      )) as Record<string, unknown>
      expect(body).toEqual({
        reason: 'Vehicle cleared the gate',
        dispatch_date: '2026-10-24',
        expected_delivery_date: '2026-10-28',
        lr_number: 'LR-204',
        vehicle_number: 'MH12AB1234',
        tracking_id: 'TRACK-204',
        location: 'Pune plant gate',
      })
    },
    POPPER,
  )

  it(
    'shows the recorded gate release time on the tracking timeline',
    async () => {
      const detail = {
        ...dispatch({ status: 'DISPATCHED' }),
        packing_order_id: null,
        dispatched_at: '2026-10-24T12:30:00Z',
        transport_name: null,
        lr_number: 'LR-204',
        vehicle_number: 'MH12AB1234',
        driver_name: null,
        driver_mobile: null,
        freight_amount: '0',
        freight_paid_by: null,
        eway_bill_number: null,
        invoice_document_id: null,
        delivery_confirmed_by: null,
        delivery_note: null,
        delay_reason: null,
        delay_notified_at: null,
        notes: null,
        version: 1,
        lines: [],
      }
      const { user } = await openActions('DISPATCHED', [P.DISPATCH_VIEW], {
        [`GET /dispatch/${DISPATCH_ID}`]: ok(detail),
        [`GET /dispatch/${DISPATCH_ID}/tracking-events`]: ok([]),
      })

      await user.click(screen.getByRole('menuitem', { name: /tracking timeline/i }))
      const gateTime = await screen.findByText('Gate time')
      expect(gateTime.parentElement).toHaveTextContent('2026')
    },
    POPPER,
  )

  it(
    'withholds the release from an account that may only update',
    async () => {
      // `POST /dispatch/{id}/dispatch` is guarded by `dispatch.create`, not
      // `dispatch.update`: sending the goods is the stock-moving act
      // (service.py `dispatch_consignment`), while staging, in-transit and
      // cancel are edits. Gated on update, the holder of create never saw the
      // only action that moves stock and the updater was refused with a 403.
      await openActions('READY_FOR_DISPATCH', [P.DISPATCH_VIEW, P.DISPATCH_UPDATE])

      const items = menuItems()
      expect(items).toContain('Cancel dispatch')
      expect(items).not.toContain('Dispatch')
    },
    POPPER,
  )

  it(
    'confirms a delivery with the payload that endpoint actually takes',
    async () => {
      const { user, fetch } = await openActions(
        'DISPATCHED',
        [P.DISPATCH_VIEW, P.DISPATCH_UPDATE, P.DISPATCH_CONFIRM_DELIVERY],
        { [`POST /dispatch/${DISPATCH_ID}/deliver`]: ok(null) },
        // The fixture's September date is now in the past, and a late arrival
        // refuses to post until a delay reason is given. That gate has its own
        // test; this one is the on-time payload.
        { expected_delivery_date: promisedAhead(14) },
      )

      const items = menuItems()
      expect(items).toContain('Mark in transit')
      expect(items).toContain('Confirm delivery')
      // It has gone. There is stock outside the warehouse, so the abandonment
      // path - which returns nothing, because nothing moved - is not offered.
      expect(items).not.toContain('Cancel dispatch')

      await user.click(screen.getByRole('menuitem', { name: 'Confirm delivery' }))
      await user.click(await screen.findByRole('button', { name: 'Confirm delivery' }))

      const body = (await waitFor(() =>
        fetch.requestFor('POST', `/dispatch/${DISPATCH_ID}/deliver`),
      )) as Record<string, unknown>

      // The four transitions do NOT share a payload. This one records a note
      // against the delivery and refuses a `reason`; an empty note is omitted
      // rather than sent as an empty string, which the API bounds.
      expect(body).toEqual({ source: 'MANUAL' })
      expect(body).not.toHaveProperty('reason')
      expect(body).not.toHaveProperty('delivery_note')
    },
    POPPER,
  )

  it(
    'asks for the delay reason the API insists on when the promised date has passed',
    async () => {
      // `confirm_delivery` refuses a late arrival without `delay_reason`, and
      // `DeliveryConfirmation` is extra="forbid", so nothing else can carry it.
      // Without a field for it the operator got the API's rejection on every
      // attempt, and a late consignment could never be confirmed from here.
      const { user, fetch } = await openActions(
        'IN_TRANSIT',
        [P.DISPATCH_VIEW, P.DISPATCH_CONFIRM_DELIVERY],
        {
          'GET /dispatch': page([
            dispatch({ status: 'IN_TRANSIT', expected_delivery_date: '2026-08-01' }),
          ]),
          [`POST /dispatch/${DISPATCH_ID}/deliver`]: ok(null),
        },
      )

      await user.click(screen.getByRole('menuitem', { name: 'Confirm delivery' }))
      await user.click(await screen.findByRole('button', { name: 'Confirm delivery' }))

      expect(await screen.findByText(/arrived after its promised date/i)).toBeVisible()
      expect(fetch.calls.some((call) => call.path.endsWith('/deliver'))).toBe(false)

      await user.type(screen.getByLabelText(/delay reason/i), 'Held at the transit hub')
      await user.click(screen.getByRole('button', { name: 'Confirm delivery' }))

      const body = (await waitFor(() =>
        fetch.requestFor('POST', `/dispatch/${DISPATCH_ID}/deliver`),
      )) as Record<string, unknown>
      // The reason travels as `delay_reason`; the optional note, left empty,
      // is still omitted rather than sent as an empty string.
      expect(body).toEqual({ source: 'MANUAL', delay_reason: 'Held at the transit hub' })
    },
    POPPER,
  )

  it(
    'withholds the delivery confirmation from an account that may only update',
    async () => {
      // The gate that must not be folded into `dispatch.update`: attesting that
      // a consignment arrived is a different act from editing it, and it is the
      // one that closes the customer's claim window.
      await openActions('DISPATCHED', [P.DISPATCH_VIEW, P.DISPATCH_UPDATE])

      const items = menuItems()
      expect(items).toContain('Mark in transit')
      expect(items).not.toContain('Confirm delivery')
    },
    POPPER,
  )

  it(
    'offers a settled consignment nothing but its history',
    async () => {
      // Delivered, received, GRN-completed and cancelled are terminal states on
      // this screen. Anything further happens in quality or on the receipt.
      await openActions('DELIVERED', [
        P.DISPATCH_VIEW,
        P.DISPATCH_UPDATE,
        P.DISPATCH_CONFIRM_DELIVERY,
      ])

      expect(menuItems()).toEqual(['Tracking timeline'])
    },
    POPPER,
  )

  it(
    'offers a reader the timeline and no transitions at all',
    async () => {
      await openActions('NOT_DISPATCHED', [P.DISPATCH_VIEW])

      expect(menuItems()).toEqual(['Tracking timeline'])
    },
    POPPER,
  )
})

describe('creating a consignment', () => {
  async function openCreate() {
    const rendered = renderScreen(<DispatchPage />, {
      permissions: [P.DISPATCH_VIEW, P.DISPATCH_CREATE, P.WAREHOUSE_VIEW],
      routes: {
        'GET /dispatch': page([]),
        'GET /warehouses': page([
          { id: '01a00000-0000-7000-8000-0000000000w1', code: 'WH1', name: 'Main store' },
        ]),
      },
    })
    await rendered.user.click(await screen.findByRole('button', { name: /create dispatch/i }))
    await screen.findByText('Create a dispatch')
    return rendered
  }

  it('insists on something the consignment can be traced by', async () => {
    // A consignment that leaves the gate with no tracking id, no LR number and
    // no vehicle is one nobody can chase when the customer rings - and the
    // tracking board downstream is only as good as what is captured here.
    const { user, fetch } = await openCreate()

    await user.click(screen.getByRole('button', { name: /^create dispatch$/i, hidden: false }))

    expect(
      await screen.findByText(/record a tracking id, an lr number or a vehicle number/i),
    ).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('will not post a sales consignment with no customer', async () => {
    const { user } = await openCreate()

    await user.type(screen.getByRole('textbox', { name: 'Tracking id' }), 'BD-77123')
    await user.click(screen.getByRole('button', { name: /^create dispatch$/i, hidden: false }))

    expect(await screen.findByText(/must name the customer it is going to/i)).toBeVisible()
  })

  it('explains itself rather than failing silently without warehouse access', async () => {
    // `warehouse_id` is required by `DispatchCreate`, and the picker that
    // supplies it needs `warehouse.view`. Rendering an empty select would look
    // like a broken screen; saying who to ask is actionable.
    const { user } = renderScreen(<DispatchPage />, {
      permissions: [P.DISPATCH_VIEW, P.DISPATCH_CREATE],
      routes: { 'GET /dispatch': page([]) },
    })

    await user.click(await screen.findByRole('button', { name: /create dispatch/i }))
    expect(await screen.findByText(/your role cannot read the warehouse list/i)).toBeVisible()
  })
})

describe('the tracking board', () => {
  it('names the party and the direction the movement is going', async () => {
    // The board joins both directions - material out to a customer and material
    // in from a vendor - which is why the party is typed rather than named
    // "customer".
    const { user } = renderScreen(<DispatchPage />, {
      permissions: [P.DISPATCH_VIEW],
      routes: {
        'GET /dispatch': page([dispatch()]),
        'GET /dispatch/tracking': page([
          trackingRow({ party_type: 'VENDOR', party_name: 'Rolled Metals', qc_pending: true }),
        ]),
      },
    })

    await screen.findByText('DSP-0001')
    await user.click(screen.getByRole('tab', { name: /tracking board/i }))

    const row = (await screen.findByText('Rolled Metals')).closest('tr')
    expect(row).not.toBeNull()
    expect(within(row!).getByText(/Vendor - DSP-0001/)).toBeVisible()
    expect(within(row!).getByText('QC pending')).toBeVisible()
  })

  it('says nothing is moving rather than showing a blank board', async () => {
    const { user } = renderScreen(<DispatchPage />, {
      permissions: [P.DISPATCH_VIEW],
      routes: {
        'GET /dispatch': page([dispatch()]),
        'GET /dispatch/tracking': page([]),
      },
    })

    await screen.findByText('DSP-0001')
    await user.click(screen.getByRole('tab', { name: /tracking board/i }))

    expect(await screen.findByText('Nothing in transit')).toBeVisible()
  })
})
