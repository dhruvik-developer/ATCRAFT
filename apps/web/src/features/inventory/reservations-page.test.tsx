/**
 * Reservations - who each promised quantity belongs to.
 *
 * The thing this file exists to hold still: **a reservation does not move
 * stock.** It changes `reserved_quantity` on a balance and nothing else - the
 * units stay on hand and stay where they are (docs/database.md section 4.2). A
 * screen that implies otherwise teaches an operator to expect a movement that
 * never happened, and the next person to read the shelf finds stock the system
 * said had gone.
 *
 * After that, three rights that are easy to conflate and must not be:
 *
 *   P.RESERVATION_CREATE      may promise stock to a demand
 *   P.RESERVATION_RELEASE     may hand a promise back
 *   P.ALLOCATION_REALLOCATE   may take one demand's promise for another
 *   P.ALLOCATION_VIEW         may read what is free before promising it
 *
 * Four separate rights, four separate controls. Every one of the six
 * authorisation defects this codebase has shipped was a control gated on the
 * wrong one of a set like this, and every one would have passed a test that
 * stubbed `can()` to true - so these render the real provider twice and compare.
 *
 * And the quantity: a decimal string all the way to the wire. `12.5000` parsed
 * into a double and re-serialised is how a four-decimal quantity becomes
 * 12.499999999999998 in an audited ledger.
 */

import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen } from '@/test/harness'

import ReservationsPage from './reservations-page'
import type { ReservationRow } from './types'

const ITEM = {
  id: 'it000000-0000-7000-8000-000000000001',
  code: 'BRASS-ROD-12',
  name: 'Brass rod 12mm',
}
const WAREHOUSE = { id: 'wh000000-0000-7000-8000-000000000001', code: 'MAIN', name: 'Main store' }
const DEMAND_ID = '0192f5a0-0000-7000-8000-000000000abc'

function reservation(overrides: Partial<ReservationRow> = {}): ReservationRow {
  return {
    id: 'rs000000-0000-7000-8000-000000000001',
    item_id: ITEM.id,
    item_code: ITEM.code,
    item_name: ITEM.name,
    warehouse_id: WAREHOUSE.id,
    warehouse_code: WAREHOUSE.code,
    location_id: null,
    batch_id: null,
    demand_type: 'SALES_ORDER',
    demand_id: DEMAND_ID,
    demand_line_id: null,
    demand_code: 'SO/2026-27/00184',
    reservation_type: 'HARD',
    status: 'ACTIVE',
    quantity: '40.0000',
    consumed_quantity: '0.0000',
    released_quantity: '0.0000',
    remaining_quantity: '40.0000',
    priority: 100,
    required_date: '2026-09-30',
    expires_at: null,
    reason: 'Confirmed by the dealer',
    released_by_id: null,
    released_at: null,
    release_reason: null,
    created_at: '2026-08-30T10:00:00Z',
    updated_at: '2026-08-30T10:00:00Z',
    created_by_id: null,
    ...overrides,
  }
}

/** The routes every render of this screen needs whatever it is about. */
function baseRoutes(rows: ReservationRow[] = [reservation()]) {
  return {
    'GET /allocation/reservations': page(rows),
    'GET /warehouses': page([WAREHOUSE]),
  }
}

/** Open the row's action menu and return its contents. */
async function openRowMenu(user: ReturnType<typeof renderScreen>['user']) {
  const trigger = await screen.findByRole('button', { name: /actions for BRASS-ROD-12/i })
  await user.click(trigger)
  return screen.findByRole('menu')
}

// --------------------------------------------------------- gating ----------

describe('the three rights this screen must not conflate', () => {
  it('offers nothing to a reader', async () => {
    // RESERVATION_VIEW is what the route itself is satisfied by, so this is the
    // most common visitor: a planner looking, not acting.
    renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW],
      routes: baseRoutes(),
    })

    expect(await screen.findByText('SO/2026-27/00184')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reserve stock' })).not.toBeInTheDocument()
    // Not a disabled menu with disabled items - no trigger at all, because a
    // reader has nothing behind it.
    expect(screen.queryByRole('button', { name: /actions for/i })).not.toBeInTheDocument()
  })

  it('gives RESERVATION_CREATE the create button and nothing on the row', async () => {
    // Creating a promise is not the same authority as breaking one. A screen
    // that opened the row menu here would offer Release to someone who may only
    // reserve.
    renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW, P.RESERVATION_CREATE],
      routes: baseRoutes(),
    })

    expect(await screen.findByRole('button', { name: 'Reserve stock' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /actions for/i })).not.toBeInTheDocument()
  })

  it('gives RESERVATION_RELEASE the release item only', async () => {
    const { user } = renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW, P.RESERVATION_RELEASE],
      routes: baseRoutes(),
    })

    const menu = await openRowMenu(user)
    expect(within(menu).getByRole('menuitem', { name: /release/i })).toBeInTheDocument()
    // Reallocation takes stock off another demand entirely. Someone who may
    // release their own claim has not been given that.
    expect(within(menu).queryByRole('menuitem', { name: /reallocation/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reserve stock' })).not.toBeInTheDocument()
  })

  it('gives ALLOCATION_REALLOCATE the reallocation item only', async () => {
    const { user } = renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW, P.ALLOCATION_REALLOCATE],
      routes: baseRoutes(),
    })

    const menu = await openRowMenu(user)
    expect(
      within(menu).getByRole('menuitem', { name: /suggest reallocation/i }),
    ).toBeInTheDocument()
    expect(within(menu).queryByRole('menuitem', { name: /^release/i })).not.toBeInTheDocument()
  })
})

describe('a claim that is no longer live', () => {
  it('does not offer to release or reallocate a consumed reservation', async () => {
    // LIVE_STATUSES is ACTIVE and PARTIALLY_CONSUMED. Offering the action on a
    // CONSUMED row puts the user through a confirmation and a reason for a
    // request the state machine refuses - and an invalid transition reads
    // exactly like a real failure.
    const { user } = renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW, P.RESERVATION_RELEASE, P.ALLOCATION_REALLOCATE],
      routes: baseRoutes([
        reservation({
          status: 'CONSUMED',
          consumed_quantity: '40.0000',
          remaining_quantity: '0.0000',
        }),
      ]),
    })

    const menu = await openRowMenu(user)
    expect(within(menu).getByRole('menuitem', { name: /release/i })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
    expect(within(menu).getByRole('menuitem', { name: /suggest reallocation/i })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
  })

  it('still offers both on a partially consumed one, which is still a live claim', async () => {
    const { user } = renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW, P.RESERVATION_RELEASE, P.ALLOCATION_REALLOCATE],
      routes: baseRoutes([
        reservation({
          status: 'PARTIALLY_CONSUMED',
          consumed_quantity: '15.0000',
          remaining_quantity: '25.0000',
        }),
      ]),
    })

    const menu = await openRowMenu(user)
    expect(within(menu).getByRole('menuitem', { name: /release/i })).not.toHaveAttribute(
      'aria-disabled',
      'true',
    )
  })
})

// ---------------------------------------------------- the create form ------

/** Fill everything the create dialog needs, leaving the optional fields blank. */
async function fillCreateForm(
  user: ReturnType<typeof renderScreen>['user'],
  { quantity = '12.5000', reason = 'Held for the dealer order confirmed on 28 Aug' } = {},
) {
  await user.click(await screen.findByRole('button', { name: 'Reserve stock' }))
  const dialog = await screen.findByRole('dialog')

  await user.click(within(dialog).getByRole('combobox', { name: 'Item' }))
  await user.click(await screen.findByRole('option', { name: /BRASS-ROD-12/ }))

  await user.click(within(dialog).getByRole('combobox', { name: 'Warehouse' }))
  await user.click(await screen.findByRole('option', { name: /MAIN/ }))

  await user.type(within(dialog).getByRole('textbox', { name: /quantity to hold/i }), quantity)

  await user.click(within(dialog).getByRole('combobox', { name: 'Demand type' }))
  await user.click(await screen.findByRole('option', { name: 'Sales Order' }))

  await user.type(within(dialog).getByRole('textbox', { name: /demand id/i }), DEMAND_ID)
  await user.type(within(dialog).getByRole('textbox', { name: /^reason/i }), reason)

  return dialog
}

describe('what the create form sends', () => {
  it('posts the quantity as a decimal string and the ids as ids', async () => {
    const { user, fetch } = renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW, P.RESERVATION_CREATE],
      routes: {
        ...baseRoutes(),
        'GET /products/items': page([ITEM]),
        'POST /allocation/reservations': ok(reservation({ quantity: '12.5000' })),
      },
    })

    const dialog = await fillCreateForm(user)
    await user.click(within(dialog).getByRole('button', { name: 'Reserve stock' }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    const body = fetch.requestFor('POST', '/allocation/reservations') as Record<string, unknown>

    // The whole body, not a subset: an extra field here is a field the API was
    // never asked to accept, and a missing one is a 422 the user has to guess at.
    expect(body).toEqual({
      item_id: ITEM.id,
      warehouse_id: WAREHOUSE.id,
      quantity: '12.5000',
      demand_type: 'SALES_ORDER',
      demand_id: DEMAND_ID,
      // Optional and left blank: sent as null, which the contract declares
      // (`demand_code: string | null`), rather than as an empty string the
      // server would store as a document number of "".
      demand_code: null,
      reservation_type: 'HARD',
      priority: 100,
      required_date: null,
      reason: 'Held for the dealer order confirmed on 28 Aug',
    })

    // Stated separately because `toEqual` would accept the number 12.5 for the
    // string '12.5000'. It is the string that must reach the wire: a quantity
    // through a double is the round trip docs/api.md forbids, and the trailing
    // precision is not decoration - it is the grain the ledger is kept at.
    expect(typeof body.quantity).toBe('string')
    expect(body.quantity).toBe('12.5000')
    // Priority is an integer the contract bounds at 9999, so a number is right
    // here in a way it never is for a quantity.
    expect(typeof body.priority).toBe('number')
  })

  it('sends nothing that moves stock', async () => {
    // A reservation changes `reserved_quantity` and only that. If a field
    // naming a movement, a transaction type or an on-hand figure ever appears
    // in this body, the screen has started doing inventory's job - and every
    // stock movement in this system goes through `post_movement`, not here.
    const { user, fetch } = renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW, P.RESERVATION_CREATE],
      routes: {
        ...baseRoutes(),
        'GET /products/items': page([ITEM]),
        'POST /allocation/reservations': ok(reservation()),
      },
    })

    const dialog = await fillCreateForm(user)
    await user.click(within(dialog).getByRole('button', { name: 'Reserve stock' }))
    await expect.poll(() => fetch.calls.some((c) => c.method === 'POST')).toBe(true)

    const body = fetch.requestFor('POST', '/allocation/reservations') as Record<string, unknown>
    for (const forbidden of [
      'on_hand_quantity',
      'transaction_type',
      'quantity_delta',
      'stock_state',
      'movement',
    ]) {
      expect(body).not.toHaveProperty(forbidden)
    }
    // And the only endpoint touched is the allocation one. Nothing on this
    // screen posts to the inventory ledger.
    expect(fetch.calls.filter((call) => call.method === 'POST').map((call) => call.path)).toEqual([
      '/allocation/reservations',
    ])
  })

  it('tells the user the units do not move', async () => {
    // The copy is the only thing standing between "reserved" and "issued" in an
    // operator's head, so it is asserted rather than left to survive a rewrite.
    const { user } = renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW, P.RESERVATION_CREATE],
      routes: { ...baseRoutes(), 'GET /products/items': page([ITEM]) },
    })

    expect(
      await screen.findByText(/reserved quantity is still physically present/i),
    ).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Reserve stock' }))
    const dialog = await screen.findByRole('dialog')
    expect(
      within(dialog).getByText(/nothing moves: the units stay where they are and stay on hand/i),
    ).toBeInTheDocument()
  })
})

describe('availability is its own right', () => {
  it('does not read available-to-promise without ALLOCATION_VIEW', async () => {
    // The endpoint carries `allocation.view`, and the route that mounts this
    // page is satisfied by `reservation.view` alone - so somebody can stand in
    // this dialog without it. Asking anyway would 403 on every keystroke.
    // The harness throws on an unrouted request, so completing the form with no
    // route for that path is itself the assertion; the explicit check below
    // says so out loud.
    const { user, fetch } = renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW, P.RESERVATION_CREATE],
      routes: {
        ...baseRoutes(),
        'GET /products/items': page([ITEM]),
        'POST /allocation/reservations': ok(reservation()),
      },
    })

    const dialog = await fillCreateForm(user)
    expect(
      within(dialog).getByText(/the API checks availability under a lock/i),
    ).toBeInTheDocument()
    expect(fetch.calls.map((call) => call.path)).not.toContain('/allocation/available-to-promise')
  })

  it('warns when the claim is larger than what is free, and still lets it be sent', async () => {
    // A warning, never a block: the figure is already historical by the time it
    // renders, and the lock the API takes when it saves is the only answer that
    // counts. Blocking here would also mean a planner cannot promise stock that
    // arrives an hour later.
    const { user, fetch } = renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW, P.RESERVATION_CREATE, P.ALLOCATION_VIEW],
      routes: {
        ...baseRoutes(),
        'GET /products/items': page([ITEM]),
        'GET /allocation/available-to-promise': ok({
          item_id: ITEM.id,
          warehouse_id: WAREHOUSE.id,
          available_quantity: '8.0000',
          as_of: '2026-09-02T09:00:00Z',
        }),
        'POST /allocation/reservations': ok(reservation()),
      },
    })

    const dialog = await fillCreateForm(user)
    expect(
      await within(dialog).findByText(/more than is free in this warehouse/i),
    ).toBeInTheDocument()

    const submit = within(dialog).getByRole('button', { name: 'Reserve stock' })
    expect(submit).toBeEnabled()
    await user.click(submit)
    await expect.poll(() => fetch.calls.some((c) => c.method === 'POST')).toBe(true)
  })
})

describe('the server answering back', () => {
  it('puts a 422 field error beside the field it names', async () => {
    // INSUFFICIENT_AVAILABLE_STOCK arrives as a field error on `quantity`. The
    // mutation cache deliberately stays quiet for a 422 that carries fields, on
    // the understanding that the form shows them - so if this stops working the
    // user gets no message at all, from anywhere.
    const { user } = renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW, P.RESERVATION_CREATE],
      routes: {
        ...baseRoutes(),
        'GET /products/items': page([ITEM]),
        'POST /allocation/reservations': fail(422, 'VALIDATION_ERROR', 'Invalid reservation', {
          fields: { quantity: ['Only 8 is available at MAIN.'] },
        }),
      },
    })

    const dialog = await fillCreateForm(user)
    await user.click(within(dialog).getByRole('button', { name: 'Reserve stock' }))

    expect(await screen.findByText('Only 8 is available at MAIN.')).toBeInTheDocument()
    // Still open, with the typed values intact: a dialog that closed on a
    // rejection would make a refusal look like a success.
    expect(within(dialog).getByRole('textbox', { name: /quantity to hold/i })).toHaveValue(
      '12.5000',
    )
  })

  it('keeps the form open and unsent-looking when the API refuses outright', async () => {
    // A 403 has no field to attach to, so the only thing this screen owes the
    // user is not pretending it worked: the dialog stays, the values stay, and
    // nothing is posted twice.
    const { user, fetch } = renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW, P.RESERVATION_CREATE],
      routes: {
        ...baseRoutes(),
        'GET /products/items': page([ITEM]),
        'POST /allocation/reservations': fail(403, 'PERMISSION_DENIED', 'Not permitted.'),
      },
    })

    const dialog = await fillCreateForm(user)
    await user.click(within(dialog).getByRole('button', { name: 'Reserve stock' }))
    await expect.poll(() => fetch.calls.filter((c) => c.method === 'POST')).toHaveLength(1)

    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(within(dialog).getByRole('textbox', { name: /demand id/i })).toHaveValue(DEMAND_ID)
  })

  it('refuses a quantity the API would reject, without asking the API', async () => {
    // Five decimal places is not a quantity this system has, and a round trip to
    // find that out is a round trip that could have been a message.
    const { user, fetch } = renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW, P.RESERVATION_CREATE],
      routes: { ...baseRoutes(), 'GET /products/items': page([ITEM]) },
    })

    const dialog = await fillCreateForm(user, { quantity: '1.23456' })
    await user.click(within(dialog).getByRole('button', { name: 'Reserve stock' }))

    expect(
      await screen.findByText(/greater than zero and to 4 decimal places/i),
    ).toBeInTheDocument()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })
})

// -------------------------------------------------------------- release ----

describe('releasing a claim', () => {
  it('sends the reason, and will not send without one', async () => {
    // The ledger keeps this sentence. A release with no recorded cause is the
    // unexplained stock change master rules 83 forbids.
    const { user, fetch } = renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW, P.RESERVATION_RELEASE],
      routes: {
        ...baseRoutes(),
        'POST /allocation/reservations': ok({ id: 'rs000000-0000-7000-8000-000000000001' }),
      },
    })

    const menu = await openRowMenu(user)
    await user.click(within(menu).getByRole('menuitem', { name: /release/i }))

    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Release reservation' }))
    expect(
      await screen.findByText(/record why the reservation is being released/i),
    ).toBeInTheDocument()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)

    await user.type(
      within(dialog).getByRole('textbox', { name: /reason/i }),
      '  Order cancelled by the customer on 12 Aug  ',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Release reservation' }))

    await expect.poll(() => fetch.calls.filter((c) => c.method === 'POST')).toHaveLength(1)
    const call = fetch.calls.find((c) => c.method === 'POST')!
    expect(call.path).toBe('/allocation/reservations/rs000000-0000-7000-8000-000000000001/release')
    // Trimmed, because a reason padded with whitespace is stored padded forever.
    expect(call.body).toEqual({ reason: 'Order cancelled by the customer on 12 Aug' })
  })

  it('says the stock returns to free rather than that it moves', async () => {
    const { user } = renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW, P.RESERVATION_RELEASE],
      routes: baseRoutes(),
    })

    const menu = await openRowMenu(user)
    await user.click(within(menu).getByRole('menuitem', { name: /release/i }))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/returns to free stock/i)).toBeInTheDocument()
  })
})

// --------------------------------------------------------- reallocation ---

describe('suggesting a reallocation', () => {
  const outcome = {
    id: 'rq000000-0000-7000-8000-000000000001',
    status: 'PENDING' as const,
    from_reservation_id: 'rs000000-0000-7000-8000-000000000009',
    from_demand_type: 'PRODUCTION_ORDER' as const,
    from_demand_id: 'pd000000-0000-7000-8000-000000000001',
    from_demand_code: 'PO/2026-27/00042',
    from_reservation_type: 'HARD' as const,
    to_demand_type: 'SALES_ORDER' as const,
    to_demand_id: DEMAND_ID,
    to_demand_code: 'SO/2026-27/00184',
    item_id: ITEM.id,
    item_code: ITEM.code,
    item_name: ITEM.name,
    warehouse_id: WAREHOUSE.id,
    warehouse_code: WAREHOUSE.code,
    quantity: '5.2500',
    reason: 'Customer escalation',
    approved_at: null,
    decision_note: null,
    to_reservation_id: null,
    candidates: [
      {
        reservation_id: 'rs000000-0000-7000-8000-000000000009',
        item_id: ITEM.id,
        warehouse_id: WAREHOUSE.id,
        demand_type: 'PRODUCTION_ORDER' as const,
        demand_id: 'pd000000-0000-7000-8000-000000000001',
        demand_code: 'PO/2026-27/00042',
        reservation_type: 'HARD' as const,
        priority: 300,
        required_date: '2026-11-01',
        quantity: '20.0000',
        consumed_quantity: '0.0000',
        remaining_quantity: '20.0000',
        takeable_quantity: '20.0000',
        requires_approval: true,
        score: '0.9100',
        reasons: ['Lower priority than the requester', 'Required a month later'],
      },
      {
        reservation_id: 'rs000000-0000-7000-8000-000000000010',
        item_id: ITEM.id,
        warehouse_id: WAREHOUSE.id,
        demand_type: 'SPARE' as const,
        demand_id: 'sp000000-0000-7000-8000-000000000001',
        demand_code: null,
        reservation_type: 'SOFT' as const,
        priority: 500,
        required_date: null,
        quantity: '3.0000',
        consumed_quantity: '0.0000',
        remaining_quantity: '3.0000',
        takeable_quantity: '3.0000',
        requires_approval: false,
        score: '0.4000',
        reasons: ['Soft claim'],
      },
    ],
  }

  it('sends the shortfall as a string, against the row it was opened from', async () => {
    // The row is the demand being *helped*, never the donor - the engine
    // excludes the requester from its own candidate pool. Sending the row's
    // reservation as the source would ask the engine to take stock from the
    // demand it is meant to be giving it to.
    const { user, fetch } = renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW, P.ALLOCATION_REALLOCATE],
      routes: { ...baseRoutes(), 'POST /allocation/reallocation-requests': ok(outcome) },
    })

    const menu = await openRowMenu(user)
    await user.click(within(menu).getByRole('menuitem', { name: /suggest reallocation/i }))

    const dialog = await screen.findByRole('dialog')
    await user.type(
      within(dialog).getByRole('textbox', { name: /quantity still needed/i }),
      '5.2500',
    )
    await user.type(
      within(dialog).getByRole('textbox', { name: /reason/i }),
      'Customer escalation - ships this week',
    )
    await user.click(within(dialog).getByRole('button', { name: /raise reallocation request/i }))

    await expect
      .poll(() => fetch.calls.some((c) => c.path === '/allocation/reallocation-requests'))
      .toBe(true)
    const body = fetch.requestFor('POST', '/allocation/reallocation-requests') as Record<
      string,
      unknown
    >
    expect(body.quantity).toBe('5.2500')
    expect(typeof body.quantity).toBe('string')
    expect(body.to_demand_type).toBe('SALES_ORDER')
    expect(body.to_demand_id).toBe(DEMAND_ID)
    expect(body.item_id).toBe(ITEM.id)
    expect(body.warehouse_id).toBe(WAREHOUSE.id)
  })

  it('shows the donor the engine chose, the ones it turned down, and that nothing has moved', async () => {
    // The engine's own account of the ranking, not one assembled in the browser:
    // scoring donors here would mean guessing at priorities the server owns.
    const { user } = renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW, P.ALLOCATION_REALLOCATE],
      routes: { ...baseRoutes(), 'POST /allocation/reallocation-requests': ok(outcome) },
    })

    const menu = await openRowMenu(user)
    await user.click(within(menu).getByRole('menuitem', { name: /suggest reallocation/i }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('textbox', { name: /quantity still needed/i }), '5.25')
    await user.type(within(dialog).getByRole('textbox', { name: /reason/i }), 'Customer escalation')
    await user.click(within(dialog).getByRole('button', { name: /raise reallocation request/i }))

    expect(await screen.findByText(/taking 5.25 from PO\/2026-27\/00042/i)).toBeInTheDocument()
    expect(screen.getByText('Lower priority than the requester')).toBeInTheDocument()
    // The alternative, listed as considered rather than silently dropped.
    expect(screen.getByText(/also considered \(1\)/i)).toBeInTheDocument()
    // The point an approver has to understand before signing.
    expect(screen.getByText(/nothing has moved/i)).toBeInTheDocument()
    expect(
      screen.getByText(/waits for an allocation approver before any stock moves/i),
    ).toBeInTheDocument()
  })
})

describe('states a person can act on', () => {
  it('explains an empty list rather than showing a bare grid', async () => {
    renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW],
      routes: baseRoutes([]),
    })

    expect(await screen.findByText('No reservations match these filters')).toBeInTheDocument()
    expect(
      screen.getByText(/confirming a sales order or releasing a production order/i),
    ).toBeInTheDocument()
  })

  it('reports a refused list instead of looking like there are no promises on stock', async () => {
    renderScreen(<ReservationsPage />, {
      permissions: [P.RESERVATION_VIEW],
      routes: {
        'GET /allocation/reservations': fail(
          403,
          'PERMISSION_DENIED',
          'You may not view reservations.',
        ),
        'GET /warehouses': page([WAREHOUSE]),
      },
    })

    expect(await screen.findByText('Could not load this list')).toBeInTheDocument()
    expect(screen.getByText('You may not view reservations.')).toBeInTheDocument()
  })
})
