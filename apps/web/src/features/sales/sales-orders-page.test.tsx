/**
 * The order book, the dialog that raises an order, its line editor, and the
 * screen where an order is confirmed.
 *
 * Four properties are worth the cost of a screen test here.
 *
 * **Confirm is gated on `sales_order.update`, not `sales_order.approve`.**
 * `POST /sales/{id}/confirm` declares `Permission.SALES_ORDER_UPDATE`. Asking
 * for `.approve` on the button hid Confirm from every order clerk who was
 * allowed to press it and offered it to approvers the API would refuse - a
 * defect that is invisible to `tsc`, invisible in a screenshot, and would pass
 * any test that stubbed `can()` to true. The harness runs the real
 * `AuthProvider`, so the paired renders below are the genuine article.
 *
 * **What the form sends.** Quantities, prices and discounts are decimal
 * *strings* the whole way to the wire; `priority` is an integer rank the API
 * bounds to 1..9; an empty unit price is `null` so the API prices the line from
 * the customer's list, which is a different instruction from "free of charge".
 *
 * **The state machine decides which buttons exist.** `isConfirmable` is DRAFT
 * only and `isCancellable` stops at ALLOCATED, so a confirmed order offers no
 * Confirm and a dispatched one offers no Cancel however many rights the reader
 * holds. Offering either walks the user through a confirmation that ends in a
 * 422 with the order exactly where it was.
 *
 * **A refusal has to reach the user.** The confirm and cancel mutations are
 * fired from dialogs, not forms, so the 422 the query client deliberately keeps
 * quiet about had nowhere to render and appeared nowhere at all.
 *
 * The `<Select>` filters on the list are driven through the URL rather than
 * clicked: that is where these controls keep their state, and each assertion
 * closes the loop by reading the request the URL produced.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Toaster } from '@/components/ui/toaster'
import { toDateInput } from '@/lib/format'
import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type FetchRecorder, type RouteTable } from '@/test/harness'

import SalesOrderDetailPage from './sales-order-detail-page'
import SalesOrdersPage from './sales-orders-page'
import type {
  AllocationLineStatus,
  SalesOrderAllocation,
  SalesOrderDetail,
  SalesOrderLine,
  SalesOrderRow,
} from './sales-types'

// ------------------------------------------------------------- fixtures ----

const ORDER_ID = '01a00000-0000-7000-8000-00000000so01'
const CUSTOMER_ID = '01a00000-0000-7000-8000-0000000cust1'
const WAREHOUSE_ID = '01a00000-0000-7000-8000-00000000wh01'
const ITEM_ID = '01a00000-0000-7000-8000-0000000item1'
const UOM_ID = '01a00000-0000-7000-8000-00000000uom1'
const LINE_ID = '01a00000-0000-7000-8000-000000000ln1'

function orderRow(overrides: Partial<SalesOrderRow> = {}): SalesOrderRow {
  return {
    id: ORDER_ID,
    code: 'SO-0001',
    customer_id: CUSTOMER_ID,
    warehouse_id: WAREHOUSE_ID,
    branch_id: null,
    status: 'DRAFT',
    order_date: '2026-09-01',
    required_date: '2026-09-30',
    promised_date: null,
    priority: 5,
    is_urgent: false,
    currency: 'INR',
    subtotal: '2500.0000',
    discount_amount: '0.0000',
    tax_amount: '450.0000',
    total_amount: '2950.0000',
    customer_po_number: 'CPO-88',
    place_of_supply_state_code: '27',
    is_interstate: false,
    requires_approval: false,
    approved_at: null,
    approved_by_id: null,
    version: 1,
    created_at: '2026-09-01T05:00:00Z',
    updated_at: '2026-09-01T05:00:00Z',
    ...overrides,
  }
}

function orderLine(overrides: Partial<SalesOrderLine> = {}): SalesOrderLine {
  // Spread last, and the result asserted: `Partial<T>` makes every optional
  // property `T[K] | undefined`, so `{...base, ...overrides}` widens a
  // `string | null` field to include `undefined` even when the caller passed
  // nothing for it. The runtime value is exactly the base object.
  return {
    id: LINE_ID,
    sales_order_id: ORDER_ID,
    line_number: 1,
    item_id: ITEM_ID,
    item_variant_id: null,
    description: 'Brass hinge 4in',
    quantity: '10.0000',
    uom_id: UOM_ID,
    unit_price: '250.0000',
    discount_percentage: '0.0000',
    discount_amount: '0.0000',
    tax_rate: '18.0000',
    tax_amount: '450.0000',
    cgst_amount: '225.0000',
    sgst_amount: '225.0000',
    igst_amount: '0.0000',
    line_total: '2950.0000',
    allocated_quantity: '0.0000',
    dispatched_quantity: '0.0000',
    invoiced_quantity: '0.0000',
    required_date: null,
    reservation_id: null,
    status: 'PENDING',
    ...overrides,
  } as SalesOrderLine
}

function orderDetail(overrides: Partial<SalesOrderDetail> = {}): SalesOrderDetail {
  return {
    ...orderRow(),
    billing_address_id: null,
    shipping_address_id: null,
    transporter_id: null,
    cancellation_reason: null,
    notes: null,
    lines: [orderLine()],
    ...overrides,
  }
}

const CUSTOMER = {
  id: CUSTOMER_ID,
  code: 'C-0001',
  name: 'Acme Hardware',
  legal_name: null,
  customer_type: 'CUSTOMER',
  price_list_type: 'SALE',
  gstin: null,
  credit_limit: '500000.0000',
  credit_days: 30,
  outstanding_amount: '0.0000',
  is_active: true,
  is_blocked: false,
  assigned_sales_user_id: null,
  created_at: '2026-01-01T00:00:00Z',
}

const ITEM = {
  id: ITEM_ID,
  code: 'FG-HINGE-4',
  name: 'Brass hinge 4in',
  base_uom_id: UOM_ID,
  is_active: true,
}

const WAREHOUSE = { id: WAREHOUSE_ID, code: 'WH-MAIN', name: 'Main store', is_active: true }

/**
 * A required date the form will accept whenever this suite is run.
 *
 * The form refuses a required date before the order date, and the order date
 * defaults to today - so a literal date here would quietly become a failing
 * test on the day it passed.
 */
const REQUIRED_DATE = toDateInput(new Date(Date.now() + 90 * 86_400_000))

// -------------------------------------------------------------- helpers ----

type User = ReturnType<typeof renderScreen>['user']

/**
 * Wait until the screen has stopped fetching.
 *
 * The customer, warehouse and item lookups settle after the rows do, and the
 * commit that renders their names replaces the DOM a press would have landed
 * on. Two polls with no new request means the screen has stopped moving.
 */
async function settled(fetch: FetchRecorder): Promise<void> {
  let seen = -1
  await waitFor(() => {
    const now = fetch.calls.length
    const stopped = now > 0 && now === seen
    seen = now
    expect(stopped, 'the screen is still fetching').toBe(true)
  })
}

/** Wait for a control, then read it again - the node may have been replaced. */
async function stable<T extends HTMLElement>(read: () => T): Promise<T> {
  await waitFor(read)
  return read()
}

/** Records the query string of every order-book request so a test can read it. */
function bookSpy(rows: SalesOrderRow[] = [orderRow()]) {
  const queries: URLSearchParams[] = []
  return {
    queries,
    handler: ({ url }: { url: URL }) => {
      queries.push(url.searchParams)
      return page(rows)
    },
    last() {
      const params = queries[queries.length - 1]
      expect(params, 'expected the screen to have queried /sales').toBeDefined()
      return params!
    },
  }
}

/** Everything the detail screen asks for, plus whatever the test adds. */
function detailRoutes(order: SalesOrderDetail, extra: RouteTable = {}): RouteTable {
  return { [`GET /sales/${order.id}`]: ok(order), ...extra }
}

function renderDetail(options: {
  order: SalesOrderDetail
  permissions: string[]
  routes?: RouteTable
  withToaster?: boolean
}) {
  const ui = options.withToaster ? (
    <>
      <SalesOrderDetailPage />
      <Toaster />
    </>
  ) : (
    <SalesOrderDetailPage />
  )
  return renderScreen(ui, {
    permissions: options.permissions,
    route: `/sales-orders/${options.order.id}`,
    path: '/sales-orders/:orderId',
    routes: detailRoutes(options.order, options.routes),
  })
}

// ------------------------------------------------------------ the list -----

describe('the order book', () => {
  it('links every row to its own order', async () => {
    // A real link, not only a clickable row: this screen is worked from the
    // keyboard all day and a `<tr onClick>` alone is unreachable that way.
    renderScreen(<SalesOrdersPage />, {
      permissions: [P.SALES_ORDER_VIEW],
      routes: { 'GET /sales': page([orderRow()]) },
    })

    const link = await screen.findByRole('link', { name: 'SO-0001' })
    expect(link).toHaveAttribute('href', `/sales-orders/${ORDER_ID}`)
  })

  it('sends every filter to the API rather than narrowing the page in the browser', async () => {
    // Master rules 31/36: the order book runs to tens of thousands of rows and
    // the browser never receives more than the page it asked for. A screen that
    // fetched everything and filtered in a `useMemo` would look identical on
    // screen - only the request gives it away.
    const spy = bookSpy()
    const { fetch } = renderScreen(<SalesOrdersPage />, {
      permissions: [P.SALES_ORDER_VIEW],
      route:
        '/sales-orders?status=CONFIRMED&customer_id=' +
        CUSTOMER_ID +
        '&required_from=2026-09-01&required_to=2026-09-30&is_urgent=true&open_only=true',
      routes: {
        'GET /sales': spy.handler,
        [`GET /customers/${CUSTOMER_ID}`]: ok(CUSTOMER),
      },
    })

    await settled(fetch)
    const params = spy.last()
    expect(params.get('status')).toBe('CONFIRMED')
    expect(params.get('customer_id')).toBe(CUSTOMER_ID)
    // The window is on the *required* date - what MRP plans against - not on
    // when the order happened to be keyed in.
    expect(params.get('required_from')).toBe('2026-09-01')
    expect(params.get('required_to')).toBe('2026-09-30')
    expect(params.get('is_urgent')).toBe('true')
    expect(params.get('open_only')).toBe('true')
  })

  it('resolves a customer filter arriving in a bookmarked URL into a name', async () => {
    // The filter is shared as a link. Left unresolved it would read as a UUID,
    // and nobody can tell which account a UUID is.
    const { fetch } = renderScreen(<SalesOrdersPage />, {
      permissions: [P.SALES_ORDER_VIEW],
      route: `/sales-orders?customer_id=${CUSTOMER_ID}`,
      routes: {
        'GET /sales': page([orderRow()]),
        [`GET /customers/${CUSTOMER_ID}`]: ok(CUSTOMER),
      },
    })

    await settled(fetch)
    expect(await screen.findByRole('combobox', { name: 'Customer' })).toHaveTextContent(
      'C-0001 - Acme Hardware',
    )
  })

  it('clears all six filters in one navigation', async () => {
    // `setSearchParams` resolves against the params captured for *this* render,
    // not against an accumulator, so six calls in one handler all start from the
    // same snapshot and only the last survives. Clearing six filters that way
    // cleared exactly one, and the request is the only place it shows.
    const spy = bookSpy()
    const { user, fetch } = renderScreen(<SalesOrdersPage />, {
      permissions: [P.SALES_ORDER_VIEW],
      route:
        '/sales-orders?status=CONFIRMED&customer_id=' +
        CUSTOMER_ID +
        '&required_from=2026-09-01&required_to=2026-09-30&is_urgent=true&open_only=true',
      routes: {
        'GET /sales': spy.handler,
        [`GET /customers/${CUSTOMER_ID}`]: ok(CUSTOMER),
      },
    })

    await settled(fetch)
    // Anchored: the customer picker carries its own "Clear customer" button.
    await user.click(screen.getByRole('button', { name: /^clear$/i }))

    await waitFor(() => expect(spy.last().get('status')).toBeNull())
    const params = spy.last()
    for (const key of [
      'status',
      'customer_id',
      'required_from',
      'required_to',
      'is_urgent',
      'open_only',
    ]) {
      expect(params.get(key), `${key} survived the clear`).toBeNull()
    }
  })

  it('flags an order still waiting for a pricing approval', async () => {
    // A below-cost line holds the order at DRAFT until somebody signs for it.
    // Worth seeing in the book rather than after opening the order.
    renderScreen(<SalesOrdersPage />, {
      permissions: [P.SALES_ORDER_VIEW],
      routes: {
        'GET /sales': page([orderRow({ requires_approval: true, approved_at: null })]),
      },
    })

    expect(await screen.findByText('Approval')).toBeVisible()
  })

  it('does not flag one that has already been approved', async () => {
    renderScreen(<SalesOrdersPage />, {
      permissions: [P.SALES_ORDER_VIEW],
      routes: {
        'GET /sales': page([
          orderRow({ requires_approval: true, approved_at: '2026-09-02T09:00:00Z' }),
        ]),
      },
    })

    await screen.findByRole('link', { name: 'SO-0001' })
    expect(screen.queryByText('Approval')).not.toBeInTheDocument()
  })

  it('marks a required date that has passed on an order still open', async () => {
    // Lateness is stated in words next to the date, not only in a colour: the
    // number of days is what decides whether anyone has to be telephoned.
    renderScreen(<SalesOrdersPage />, {
      permissions: [P.SALES_ORDER_VIEW],
      routes: {
        'GET /sales': page([orderRow({ required_date: '2020-01-01', status: 'CONFIRMED' })]),
      },
    })

    expect(await screen.findByText(/\d+d late/)).toBeVisible()
  })

  it('leaves a passed date unmarked once the order is closed', async () => {
    // A closed order cannot be late - nothing is owed - and colouring it red
    // would send somebody chasing a shipment that went out months ago.
    renderScreen(<SalesOrdersPage />, {
      permissions: [P.SALES_ORDER_VIEW],
      routes: {
        'GET /sales': page([orderRow({ required_date: '2020-01-01', status: 'CLOSED' })]),
      },
    })

    await screen.findByRole('link', { name: 'SO-0001' })
    expect(screen.queryByText(/\d+d late/)).not.toBeInTheDocument()
  })

  it('offers New order to a principal holding sales_order.create', async () => {
    renderScreen(<SalesOrdersPage />, {
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_CREATE],
      routes: { 'GET /sales': page([orderRow()]) },
    })

    await screen.findByRole('link', { name: 'SO-0001' })
    expect(screen.getByRole('button', { name: /new order/i })).toBeVisible()
  })

  it('withholds it from a principal who may only read the book', async () => {
    renderScreen(<SalesOrdersPage />, {
      permissions: [P.SALES_ORDER_VIEW],
      routes: { 'GET /sales': page([orderRow()]) },
    })

    await screen.findByRole('link', { name: 'SO-0001' })
    expect(screen.queryByRole('button', { name: /new order/i })).not.toBeInTheDocument()
  })

  it('offers a way out of an empty book rather than a dead end', async () => {
    renderScreen(<SalesOrdersPage />, {
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_CREATE],
      routes: { 'GET /sales': page([]) },
    })

    expect(await screen.findByText('No sales orders found')).toBeVisible()
    // The header's button and the empty table's, both gated on the same right.
    expect(screen.getAllByRole('button', { name: /new order/i })).toHaveLength(2)
  })

  it('surfaces the API sentence when the book fails to load, with a retry', async () => {
    renderScreen(<SalesOrdersPage />, {
      permissions: [P.SALES_ORDER_VIEW],
      routes: {
        'GET /sales': fail(503, 'SERVICE_UNAVAILABLE', 'The order service is failing over'),
      },
    })

    // The server's own wording, not "request failed": it is the only thing that
    // tells the user whether waiting will help.
    expect(await screen.findByText('The order service is failing over')).toBeVisible()
    expect(screen.getByRole('button', { name: /try again/i })).toBeVisible()
  })
})

// -------------------------------------------------------- the order form ----

describe('what the new-order form sends', () => {
  const formPermissions = [
    P.SALES_ORDER_VIEW,
    P.SALES_ORDER_CREATE,
    P.CUSTOMER_VIEW,
    P.WAREHOUSE_VIEW,
    P.ITEM_VIEW,
  ]

  function formRoutes(extra: RouteTable = {}): RouteTable {
    // The book behind the dialog carries a row on purpose: an empty one renders
    // a second "New order" in the empty state and the query would be ambiguous.
    return {
      'GET /sales': page([orderRow()]),
      'GET /customers': page([CUSTOMER]),
      [`GET /customers/${CUSTOMER_ID}/addresses`]: page([]),
      'GET /warehouses': page([WAREHOUSE]),
      'GET /products/items': page([ITEM]),
      ...extra,
    }
  }

  /** Open the dialog and fill the header the API insists on. */
  async function openForm(user: User, fetch: FetchRecorder) {
    await settled(fetch)
    await user.click(screen.getByRole('button', { name: /new order/i }))
    await stable(() => screen.getByRole('dialog'))

    await user.click(screen.getByRole('combobox', { name: 'Customer' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /C-0001/ })))
    await user.click(screen.getByRole('combobox', { name: 'Dispatch from' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /WH-MAIN/ })))
    await user.type(screen.getByLabelText(/required date/i), REQUIRED_DATE)
  }

  /** Add one line through the line editor, which is where a line is built. */
  async function addLine(
    user: User,
    values: { quantity: string; price?: string; discount?: string },
  ) {
    await user.click(screen.getByRole('button', { name: /add line/i }))
    await stable(() => screen.getByRole('combobox', { name: 'Item' }))
    await user.click(screen.getByRole('combobox', { name: 'Item' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /FG-HINGE-4/ })))

    const quantity = screen.getByRole('textbox', { name: /quantity/i })
    await user.clear(quantity)
    await user.type(quantity, values.quantity)

    if (values.price !== undefined) {
      await user.type(screen.getByRole('textbox', { name: /unit price/i }), values.price)
    }
    if (values.discount !== undefined) {
      const discount = screen.getByRole('textbox', { name: /discount/i })
      await user.clear(discount)
      await user.type(discount, values.discount)
    }
    await user.click(screen.getByRole('button', { name: 'Add line' }))
  }

  it('posts the header and its lines with every decimal still a string', async () => {
    const { user, fetch } = renderScreen(<SalesOrdersPage />, {
      permissions: formPermissions,
      routes: formRoutes({ 'POST /sales': ok({ ...orderDetail(), code: 'SO-0002' }) }),
    })

    await openForm(user, fetch)
    await addLine(user, { quantity: '12.5', price: '340.250000' })
    await user.click(screen.getByRole('button', { name: 'Create draft order' }))

    await waitFor(() => fetch.requestFor('POST', '/sales'))
    const body = fetch.requestFor('POST', '/sales') as Record<string, unknown> & {
      lines: Record<string, unknown>[]
    }

    expect(body.customer_id).toBe(CUSTOMER_ID)
    expect(body.warehouse_id).toBe(WAREHOUSE_ID)
    expect(body.required_date).toBe(REQUIRED_DATE)
    // A rank the API bounds to 1..9, sent as the integer the contract declares -
    // "5" would be refused, and there is no named band to translate.
    expect(body.priority).toBe(5)
    expect(typeof body.priority).toBe('number')
    // Nothing was typed into these, and they are sent as null rather than "" -
    // the API's nullable columns, not empty strings that would be stored.
    expect(body.promised_date).toBeNull()
    expect(body.customer_po_number).toBeNull()
    expect(body.notes).toBeNull()
    // "The customer's default" address, which the API resolves itself.
    expect(body.billing_address_id).toBeNull()
    expect(body.shipping_address_id).toBeNull()

    expect(body.lines).toEqual([
      {
        line_number: 1,
        item_id: ITEM_ID,
        description: null,
        quantity: '12.5',
        // The item's own sale unit, chosen by leaving the unit alone.
        uom_id: null,
        unit_price: '340.250000',
        discount_percentage: '0',
        required_date: null,
        notes: null,
      },
    ])
    // Six decimal places is the contract's precision for a unit price. A float
    // here hands the API 340.25 and loses what was actually negotiated.
    expect(typeof body.lines[0]!.unit_price).toBe('string')
    expect(typeof body.lines[0]!.quantity).toBe('string')
  })

  it('leaves the unit price null so the API prices the line from the price list', async () => {
    // Empty is not zero. A zero unit price is a free-of-charge line and would be
    // invoiced as one; null means "quote this account's agreed price".
    const { user, fetch } = renderScreen(<SalesOrdersPage />, {
      permissions: formPermissions,
      routes: formRoutes({ 'POST /sales': ok({ ...orderDetail(), code: 'SO-0003' }) }),
    })

    await openForm(user, fetch)
    await addLine(user, { quantity: '4' })
    await user.click(screen.getByRole('button', { name: 'Create draft order' }))

    await waitFor(() => fetch.requestFor('POST', '/sales'))
    const body = fetch.requestFor('POST', '/sales') as { lines: Record<string, unknown>[] }
    expect(body.lines[0]!.unit_price).toBeNull()
  })

  it('renumbers the lines that survive a removal', async () => {
    // Line numbers are 1-based and must stay dense: the API rejects a set with a
    // hole in it, and the operator reads them off the printed order.
    const { user, fetch } = renderScreen(<SalesOrdersPage />, {
      permissions: formPermissions,
      routes: formRoutes({ 'POST /sales': ok({ ...orderDetail(), code: 'SO-0004' }) }),
    })

    await openForm(user, fetch)
    await addLine(user, { quantity: '1', price: '10' })
    await addLine(user, { quantity: '2', price: '20' })
    await user.click(screen.getByRole('button', { name: 'Remove line 1' }))
    await user.click(screen.getByRole('button', { name: 'Create draft order' }))

    await waitFor(() => fetch.requestFor('POST', '/sales'))
    const body = fetch.requestFor('POST', '/sales') as { lines: Record<string, unknown>[] }
    expect(body.lines).toHaveLength(1)
    expect(body.lines[0]!.line_number).toBe(1)
    expect(body.lines[0]!.quantity).toBe('2')
  })

  it('refuses to post an order with no lines at all', async () => {
    // `POST /sales` has no notion of an empty draft. Saying so here beats a 422
    // pointing at a field the form does not show - and the assertion that
    // matters is that nothing was sent.
    const { user, fetch } = renderScreen(<SalesOrdersPage />, {
      permissions: formPermissions,
      routes: formRoutes(),
    })

    await openForm(user, fetch)
    await user.click(screen.getByRole('button', { name: 'Create draft order' }))

    expect(await screen.findByText('An order needs at least one line')).toBeVisible()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })

  it('renders a rejected field against the field the API named', async () => {
    // The query client stays quiet about a 422 carrying field errors, on the
    // understanding the form renders them inline. If it does not, the save
    // simply appears to do nothing.
    const { user, fetch } = renderScreen(<SalesOrdersPage />, {
      permissions: formPermissions,
      routes: formRoutes({
        'POST /sales': fail(422, 'VALIDATION_ERROR', 'Invalid', {
          fields: { customer_po_number: ['This PO number is already on order SO-0009'] },
        }),
      }),
    })

    await openForm(user, fetch)
    await addLine(user, { quantity: '1', price: '10' })
    await user.click(screen.getByRole('button', { name: 'Create draft order' }))

    expect(await screen.findByText('This PO number is already on order SO-0009')).toBeVisible()
  })

  it('tells a user without warehouse.view instead of offering a select it cannot fill', async () => {
    // An order names the warehouse it ships from, and the lookup is a separate
    // right. Firing it regardless earns a 403 the user can do nothing about.
    const { user, fetch } = renderScreen(<SalesOrdersPage />, {
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_CREATE, P.CUSTOMER_VIEW, P.ITEM_VIEW],
      routes: { 'GET /sales': page([orderRow()]), 'GET /customers': page([CUSTOMER]) },
    })

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: /new order/i }))
    await stable(() => screen.getByRole('dialog'))

    expect(screen.getByText(/your role cannot read the warehouse list/i)).toBeVisible()
    expect(screen.queryByRole('combobox', { name: 'Dispatch from' })).not.toBeInTheDocument()
    // The absence of a `GET /warehouses` route is itself the assertion: an
    // unrouted request throws rather than quietly answering with nothing.
    expect(fetch.calls.map((call) => call.path)).not.toContain('/warehouses')
    // The schema insists on a warehouse the form cannot collect, so the submit
    // would fail against a field that is not on screen - a press that visibly
    // did nothing. It is withheld instead.
    expect(screen.getByRole('button', { name: 'Create draft order' })).toBeDisabled()
  })
})

// -------------------------------------------------------- the line editor ---

describe('the line editor', () => {
  const formPermissions = [
    P.SALES_ORDER_VIEW,
    P.SALES_ORDER_CREATE,
    P.CUSTOMER_VIEW,
    P.WAREHOUSE_VIEW,
    P.ITEM_VIEW,
  ]

  async function openLineEditor(user: User, fetch: FetchRecorder) {
    await settled(fetch)
    await user.click(screen.getByRole('button', { name: /new order/i }))
    await stable(() => screen.getByRole('dialog'))
    await user.click(screen.getByRole('button', { name: /add line/i }))
    await stable(() => screen.getByRole('combobox', { name: 'Item' }))
  }

  function editorRoutes(): RouteTable {
    return {
      'GET /sales': page([orderRow()]),
      'GET /customers': page([CUSTOMER]),
      'GET /warehouses': page([WAREHOUSE]),
      'GET /products/items': page([ITEM]),
    }
  }

  it('refuses a quantity of zero rather than sending a line nothing can fulfil', async () => {
    const { user, fetch } = renderScreen(<SalesOrdersPage />, {
      permissions: formPermissions,
      routes: editorRoutes(),
    })

    await openLineEditor(user, fetch)
    await user.click(screen.getByRole('combobox', { name: 'Item' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /FG-HINGE-4/ })))
    const quantity = screen.getByRole('textbox', { name: /quantity/i })
    await user.clear(quantity)
    await user.type(quantity, '0')
    await user.click(screen.getByRole('button', { name: 'Add line' }))

    expect(await screen.findByText('Quantity must be more than zero')).toBeVisible()
    // Still in the editor: the line was not accepted into the set behind it.
    expect(screen.getByRole('button', { name: 'Add line' })).toBeVisible()
  })

  it('refuses a discount above 100 per cent', async () => {
    // A 120% discount is a negative line total, which the invoice would carry
    // through as money owed to the customer.
    const { user, fetch } = renderScreen(<SalesOrdersPage />, {
      permissions: formPermissions,
      routes: editorRoutes(),
    })

    await openLineEditor(user, fetch)
    await user.click(screen.getByRole('combobox', { name: 'Item' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /FG-HINGE-4/ })))
    const discount = screen.getByRole('textbox', { name: /discount/i })
    await user.clear(discount)
    await user.type(discount, '120')
    await user.click(screen.getByRole('button', { name: 'Add line' }))

    expect(await screen.findByText('A discount cannot exceed 100%')).toBeVisible()
  })

  it('refuses a line with no item, because a price alone orders nothing', async () => {
    const { user, fetch } = renderScreen(<SalesOrdersPage />, {
      permissions: formPermissions,
      routes: editorRoutes(),
    })

    await openLineEditor(user, fetch)
    await user.click(screen.getByRole('button', { name: 'Add line' }))

    expect(await screen.findByText('Choose an item')).toBeVisible()
  })

  it('previews the net of discount with decimal arithmetic', async () => {
    // decimal.js, not floats: 0.1 + 0.2 on a line total is a real invoice
    // defect, and the operator is reading this figure back to the customer.
    const { user, fetch } = renderScreen(<SalesOrdersPage />, {
      permissions: formPermissions,
      routes: editorRoutes(),
    })

    await openLineEditor(user, fetch)
    const quantity = screen.getByRole('textbox', { name: /quantity/i })
    await user.clear(quantity)
    await user.type(quantity, '3')
    await user.type(screen.getByRole('textbox', { name: /unit price/i }), '100.10')
    const discount = screen.getByRole('textbox', { name: /discount/i })
    await user.clear(discount)
    await user.type(discount, '10')

    // 3 x 100.10 = 300.30, less 10% = 270.27. A float would show 270.26999...
    expect(await screen.findByText(/300\.30/)).toBeVisible()
    expect(screen.getByText(/30\.03/)).toBeVisible()
    expect(screen.getByText(/270\.27/)).toBeVisible()
  })

  it('disables the unit when the account cannot read the unit master', async () => {
    // `uom.view` is its own right. Without it the line keeps the item's own sale
    // unit, which is what the API applies for a null `uom_id` anyway - and no
    // request is fired at a lookup that would answer 403.
    const { user, fetch } = renderScreen(<SalesOrdersPage />, {
      permissions: formPermissions,
      routes: editorRoutes(),
    })

    await openLineEditor(user, fetch)
    expect(screen.getByRole('combobox', { name: /unit/i })).toBeDisabled()
    expect(fetch.calls.map((call) => call.path)).not.toContain('/products/uoms')
  })
})

// ----------------------------------------------------------- the actions ----

describe('confirming an order', () => {
  it('offers Confirm to a principal holding sales_order.update', async () => {
    // `POST /sales/{id}/confirm` declares `sales_order.update`. The button and
    // its endpoint have to name the same right.
    renderDetail({
      order: orderDetail(),
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_UPDATE],
    })

    expect(await screen.findByRole('button', { name: /^confirm$/i })).toBeEnabled()
  })

  it('withholds it from an approver who holds only sales_order.approve', async () => {
    // This is the shipped defect, pinned. Gating Confirm on `.approve` hid it
    // from every clerk allowed to press it and offered it to approvers the API
    // refuses - the confirmation would have ended in a 403 either way.
    renderDetail({
      order: orderDetail(),
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_APPROVE],
    })

    await screen.findByRole('heading', { name: 'SO-0001' })
    expect(screen.queryByRole('button', { name: /^confirm$/i })).not.toBeInTheDocument()
  })

  it('does not offer Confirm on an order that is already confirmed', async () => {
    // `isConfirmable` is DRAFT only. The state machine would refuse the second
    // confirmation, so the button is absent rather than shown and disabled.
    renderDetail({
      order: orderDetail({ status: 'CONFIRMED' }),
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_UPDATE],
    })

    await screen.findByRole('heading', { name: 'SO-0001' })
    expect(screen.queryByRole('button', { name: /^confirm$/i })).not.toBeInTheDocument()
  })

  it('disables Confirm while a below-cost line is still waiting for approval', async () => {
    // Confirming would be refused, and the person looking at the screen is
    // usually not the person who can approve it - so the reason is stated.
    renderDetail({
      order: orderDetail({ requires_approval: true, approved_at: null }),
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_UPDATE],
    })

    expect(await screen.findByRole('button', { name: /^confirm$/i })).toBeDisabled()
    expect(screen.getByText('This order is waiting for a pricing approval')).toBeVisible()
  })

  it('confirms with a reason the audit trail can record', async () => {
    const order = orderDetail()
    const { user, fetch } = renderDetail({
      order,
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_UPDATE],
      routes: {
        [`POST /sales/${ORDER_ID}/confirm`]: ok({
          order: { ...order, status: 'CONFIRMED' },
          allocation: {
            order_id: ORDER_ID,
            code: 'SO-0001',
            status: 'CONFIRMED',
            warehouse_id: WAREHOUSE_ID,
            fully_allocated: true,
            total_shortfall_lines: 0,
            lines: [],
          },
        }),
      },
    })

    await user.click(await screen.findByRole('button', { name: /^confirm$/i }))
    await user.click(await stable(() => screen.getByRole('button', { name: 'Confirm order' })))

    await waitFor(() => fetch.requestFor('POST', `/sales/${ORDER_ID}/confirm`))
    const body = fetch.requestFor('POST', `/sales/${ORDER_ID}/confirm`) as { reason: string }
    // `ConfirmRequest.reason` is bounded 3..255 by the API; a shorter one comes
    // back as a 422 against a field this dialog has no control for.
    expect(body.reason.length).toBeGreaterThanOrEqual(3)
  })

  it('says so when the API refuses the confirmation', async () => {
    // These mutations are fired from a dialog, not a form, so the 422 the query
    // client keeps quiet about had nothing to render it and appeared nowhere.
    const order = orderDetail()
    const { user } = renderDetail({
      order,
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_UPDATE],
      withToaster: true,
      routes: {
        [`POST /sales/${ORDER_ID}/confirm`]: fail(422, 'VALIDATION_ERROR', 'Invalid', {
          fields: { lines: ['Line 1 has no price and the account has no price list'] },
        }),
      },
    })

    await user.click(await screen.findByRole('button', { name: /^confirm$/i }))
    await user.click(await stable(() => screen.getByRole('button', { name: 'Confirm order' })))

    expect(
      await screen.findByText(/Line 1 has no price and the account has no price list/),
    ).toBeVisible()
  })

  it('offers Approve pricing only to a holder of sales_order.approve', async () => {
    // The separate action, which genuinely is the approver's - and which the
    // Confirm gate above must not be confused with.
    const awaiting = orderDetail({ requires_approval: true, approved_at: null })

    const withRight = renderDetail({
      order: awaiting,
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_APPROVE],
    })
    expect(await screen.findByRole('button', { name: /approve pricing/i })).toBeVisible()
    withRight.unmount()

    renderDetail({
      order: awaiting,
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_UPDATE],
    })
    await screen.findByText('This order is waiting for a pricing approval')
    expect(screen.queryByRole('button', { name: /approve pricing/i })).not.toBeInTheDocument()
  })
})

describe('cancelling an order', () => {
  it('offers Cancel on a draft to a holder of sales_order.cancel', async () => {
    renderDetail({
      order: orderDetail(),
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_CANCEL],
    })

    expect(await screen.findByRole('button', { name: /cancel order/i })).toBeVisible()
  })

  it('withholds Cancel once the goods have shipped', async () => {
    // `CANCELLABLE_STATUSES` stops at ALLOCATED: nothing that has physically
    // left can be cancelled, and the reservations to release no longer exist.
    renderDetail({
      order: orderDetail({ status: 'DISPATCHED' }),
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_CANCEL, P.SALES_ORDER_UPDATE],
    })

    await screen.findByRole('heading', { name: 'SO-0001' })
    expect(screen.queryByRole('button', { name: /cancel order/i })).not.toBeInTheDocument()
  })

  it('holds a one-character reason back rather than earning a 422 for it', async () => {
    // `CancelRequest.reason` is bounded 3..255. A one-character reason used to
    // pass this form and come back as a 422 against a field path the mutation
    // cache stays quiet about - nothing appeared on screen at all.
    const { user, fetch } = renderDetail({
      order: orderDetail(),
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_CANCEL],
    })

    await user.click(await screen.findByRole('button', { name: /cancel order/i }))
    await user.type(await stable(() => screen.getByLabelText(/reason/i)), 'x')
    await user.click(screen.getByRole('button', { name: 'Cancel order' }))

    expect(await screen.findByText(/needs at least 3 characters/i)).toBeVisible()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })

  it('sends the trimmed reason once it is long enough', async () => {
    const { user, fetch } = renderDetail({
      order: orderDetail(),
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_CANCEL],
      routes: {
        [`POST /sales/${ORDER_ID}/cancel`]: ok(orderRow({ status: 'CANCELLED' })),
      },
    })

    await user.click(await screen.findByRole('button', { name: /cancel order/i }))
    await user.type(await stable(() => screen.getByLabelText(/reason/i)), '  Customer withdrew  ')
    await user.click(screen.getByRole('button', { name: 'Cancel order' }))

    await waitFor(() => fetch.requestFor('POST', `/sales/${ORDER_ID}/cancel`))
    const body = fetch.requestFor('POST', `/sales/${ORDER_ID}/cancel`) as { reason: string }
    // Trimmed: the surrounding spaces are keystrokes, not part of what the
    // audit trail should quote back in six months.
    expect(body.reason).toBe('Customer withdrew')
  })
})

describe('editing the lines of an order', () => {
  it('offers the line actions on a draft to a holder of sales_order.update', async () => {
    renderDetail({
      order: orderDetail(),
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_UPDATE],
    })

    expect(await screen.findByRole('button', { name: 'Edit line 1' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Remove line 1' })).toBeVisible()
  })

  it('withholds them once the order is confirmed', async () => {
    // `canEditLines` is DRAFT and `sales_order.update` together. On a confirmed
    // order the API re-reserves against the set, which is not what a row-level
    // pencil implies, so the affordance goes away with the state.
    renderDetail({
      order: orderDetail({ status: 'CONFIRMED' }),
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_UPDATE],
    })

    await screen.findByRole('heading', { name: 'SO-0001' })
    expect(screen.queryByRole('button', { name: 'Edit line 1' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove line 1' })).not.toBeInTheDocument()
  })

  it('withholds them from a reader who may not update the order', async () => {
    renderDetail({
      order: orderDetail(),
      permissions: [P.SALES_ORDER_VIEW],
    })

    await screen.findByRole('heading', { name: 'SO-0001' })
    expect(screen.queryByRole('button', { name: 'Edit line 1' })).not.toBeInTheDocument()
  })

  it('patches the whole line set when one line is removed', async () => {
    // Lines are not addressable - there is no `/sales/{id}/lines` endpoint - so
    // a removal is the surviving set, renumbered, sent as one patch. Sending
    // only the removed line would be read as "replace the order with this".
    const order = orderDetail({
      lines: [orderLine(), orderLine({ id: 'line-2', line_number: 2, quantity: '4.0000' })],
    })
    const { user, fetch } = renderDetail({
      order,
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_UPDATE],
      routes: { [`PATCH /sales/${ORDER_ID}`]: ok(order) },
    })

    await user.click(await screen.findByRole('button', { name: 'Remove line 1' }))
    await user.click(await stable(() => screen.getByRole('button', { name: 'Remove line' })))

    await waitFor(() => fetch.requestFor('PATCH', `/sales/${ORDER_ID}`))
    const body = fetch.requestFor('PATCH', `/sales/${ORDER_ID}`) as {
      lines: Record<string, unknown>[]
    }
    expect(body.lines).toHaveLength(1)
    // The survivor is renumbered to 1, and carries the input fields only - the
    // computed tax and dispatched quantities the write schema refuses are gone.
    expect(body.lines[0]!.line_number).toBe(1)
    expect(body.lines[0]!.quantity).toBe('4.0000')
    expect(body.lines[0]).not.toHaveProperty('tax_amount')
    expect(body.lines[0]).not.toHaveProperty('allocated_quantity')
  })
})

describe('what the detail screen shows', () => {
  it('states the totals the API calculated rather than re-adding them', async () => {
    renderDetail({
      order: orderDetail(),
      permissions: [P.SALES_ORDER_VIEW],
    })

    expect(await screen.findByText('Grand total')).toBeVisible()
    // The intra-state split named for what it is: a customer in the same state
    // is charged CGST + SGST, and the label is how anyone checks the invoice.
    expect(screen.getByText('Tax (CGST + SGST)')).toBeVisible()
  })

  it('names the inter-state tax when the place of supply is elsewhere', async () => {
    renderDetail({
      order: orderDetail({ is_interstate: true }),
      permissions: [P.SALES_ORDER_VIEW],
    })

    expect(await screen.findByText('Tax (IGST)')).toBeVisible()
  })

  it('shows the allocation panel to a reader of the order, which is what the endpoint asks for', async () => {
    // `GET /sales/{id}/allocation-status` is declared on `sales_order.view`,
    // the same right that opens this page - not on `allocation.view`, which
    // guards the reservation screens. Gating the panel on the latter refused a
    // reader the API would have answered, and told them their role lacked a
    // right the endpoint never asks for.
    renderDetail({
      order: orderDetail({ status: 'CONFIRMED' }),
      permissions: [P.SALES_ORDER_VIEW],
      routes: {
        [`GET /sales/${ORDER_ID}/allocation-status`]: ok({
          order_id: ORDER_ID,
          code: 'SO-0001',
          status: 'CONFIRMED',
          warehouse_id: WAREHOUSE_ID,
          fully_allocated: false,
          total_shortfall_lines: 1,
          lines: [
            {
              line_id: LINE_ID,
              line_number: 1,
              item_id: ITEM_ID,
              item_code: 'FG-HINGE-4',
              ordered_quantity: '10.0000',
              allocated_quantity: '6.0000',
              reserved_quantity: '6.0000',
              dispatchable_quantity: '6.0000',
              dispatched_quantity: '0.0000',
              shortfall_quantity: '4.0000',
              available_quantity: '0.0000',
              required_date: '2026-09-30',
              reservation_id: null,
              status: 'PARTIALLY_ALLOCATED',
            },
          ],
        }),
      },
    })

    expect(await screen.findByText('Allocation')).toBeVisible()
    expect(await screen.findByText('1 line short')).toBeVisible()
    // The engine's own figures, on the line: what is reserved and what is not.
    expect(screen.getByText('Reserved')).toBeVisible()
    expect(screen.getByText(/^4(\.0+)?$/)).toBeVisible()
    expect(screen.queryByText('Allocation is not visible to your account')).not.toBeInTheDocument()
  })

  it('surfaces the API sentence when the order cannot be loaded', async () => {
    renderScreen(<SalesOrderDetailPage />, {
      permissions: [P.SALES_ORDER_VIEW],
      route: `/sales-orders/${ORDER_ID}`,
      path: '/sales-orders/:orderId',
      routes: {
        [`GET /sales/${ORDER_ID}`]: fail(404, 'NOT_FOUND', 'No such sales order'),
      },
    })

    expect(await screen.findByText('Could not load this order')).toBeVisible()
    expect(within(screen.getByRole('alert')).getByText('No such sales order')).toBeVisible()
  })
})
// ------------------------------------------------------------ fulfilment ----

/**
 * The order's allocation read, as `GET /sales/{id}/allocation-status` answers
 * it: 500 ordered, 200 reserved and dispatchable now, 300 short - the
 * proposal's own K08 case.
 */
function allocationView(
  overrides: Partial<AllocationLineStatus> = {},
  status: SalesOrderAllocation['status'] = 'CONFIRMED',
): SalesOrderAllocation {
  const line: AllocationLineStatus = {
    line_id: LINE_ID,
    line_number: 1,
    item_id: ITEM_ID,
    item_code: 'FG-HINGE-4',
    ordered_quantity: '500.0000',
    allocated_quantity: '200.0000',
    reserved_quantity: '200.0000',
    dispatchable_quantity: '200.0000',
    dispatched_quantity: '0.0000',
    shortfall_quantity: '300.0000',
    status: 'PARTIALLY_ALLOCATED',
    reservation_id: null,
    available_quantity: '0.0000',
    required_date: '2026-09-30',
    ...overrides,
  }
  const short = toDecimalString(line.shortfall_quantity) !== '0'
  return {
    order_id: ORDER_ID,
    code: 'SO-0001',
    status,
    warehouse_id: WAREHOUSE_ID,
    fully_allocated: !short,
    total_shortfall_lines: short ? 1 : 0,
    lines: [line],
  }
}

function toDecimalString(value: string): string {
  return String(Number(value))
}

describe('fulfilment on the detail screen', () => {
  const CONFIRMED = orderDetail({ status: 'CONFIRMED' })

  it('reads reserved, dispatchable-now and short straight from the engine', async () => {
    renderDetail({
      order: CONFIRMED,
      permissions: [P.SALES_ORDER_VIEW],
      routes: { [`GET /sales/${ORDER_ID}/allocation-status`]: ok(allocationView()) },
    })

    expect(await screen.findByText('Dispatchable now')).toBeVisible()
    expect(screen.getByText('Reserved')).toBeVisible()
    expect(screen.getByText('Short')).toBeVisible()
    expect((await screen.findAllByText(/^200(\.0+)?$/)).length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText(/^300(\.0+)?$/)).toBeVisible()
  })

  it('offers Dispatch on a confirmed order to a holder of dispatch.create', async () => {
    // `POST /dispatch` declares `dispatch.create`; a CONFIRMED order may ship
    // what it has reserved, so the action is there before ALLOCATED.
    renderDetail({
      order: CONFIRMED,
      permissions: [P.SALES_ORDER_VIEW, P.DISPATCH_CREATE],
      routes: { [`GET /sales/${ORDER_ID}/allocation-status`]: ok(allocationView()) },
    })

    expect(await screen.findByRole('button', { name: /^dispatch$/i })).toBeEnabled()
  })

  it('withholds Dispatch without the right, and on orders nothing can leave against', async () => {
    const { unmount } = renderDetail({
      order: CONFIRMED,
      permissions: [P.SALES_ORDER_VIEW],
      routes: { [`GET /sales/${ORDER_ID}/allocation-status`]: ok(allocationView()) },
    })
    await screen.findByRole('heading', { name: 'SO-0001' })
    expect(screen.queryByRole('button', { name: /^dispatch$/i })).not.toBeInTheDocument()
    unmount()

    for (const status of ['DRAFT', 'DISPATCHED'] as const) {
      const view = renderDetail({
        order: orderDetail({ status }),
        permissions: [P.SALES_ORDER_VIEW, P.DISPATCH_CREATE],
        routes: { [`GET /sales/${ORDER_ID}/allocation-status`]: ok(allocationView({}, status)) },
      })
      await screen.findByRole('heading', { name: 'SO-0001' })
      expect(screen.queryByRole('button', { name: /^dispatch$/i })).not.toBeInTheDocument()
      view.unmount()
    }
  })

  it('opens the dispatch dialog on the order, pre-filled from its reserved lines', async () => {
    const { user } = renderDetail({
      order: CONFIRMED,
      permissions: [P.SALES_ORDER_VIEW, P.DISPATCH_CREATE],
      routes: {
        [`GET /sales/${ORDER_ID}/allocation-status`]: ok(allocationView()),
        'GET /sales': page([orderRow({ status: 'CONFIRMED' })]),
        [`GET /customers/${CUSTOMER_ID}`]: ok(CUSTOMER),
        'GET /customers': page([CUSTOMER]),
        'GET /warehouses': page([WAREHOUSE]),
      },
    })

    await user.click(await screen.findByRole('button', { name: /^dispatch$/i }))

    const dialog = await screen.findByRole('dialog', { name: 'Dispatch SO-0001' })
    expect(await within(dialog).findByLabelText('Quantity for line 1')).toHaveValue('200')
    // Only the order's lines: nothing else can be added to this consignment.
    expect(within(dialog).queryByRole('button', { name: 'Add line' })).not.toBeInTheDocument()
  })

  it('offers the top-up while a line is short, and posts it', async () => {
    const { user, fetch } = renderDetail({
      order: CONFIRMED,
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_UPDATE],
      routes: {
        [`GET /sales/${ORDER_ID}/allocation-status`]: ok(allocationView()),
        [`POST /sales/${ORDER_ID}/allocate`]: ok(
          allocationView({ reserved_quantity: '500.0000', shortfall_quantity: '0.0000' }),
        ),
      },
    })

    await user.click(await screen.findByRole('button', { name: 'Allocate available stock' }))

    await waitFor(() =>
      expect(fetch.requestFor('POST', `/sales/${ORDER_ID}/allocate`)).toEqual({
        reason: 'Available stock allocated from the order page',
      }),
    )
  })

  it('withholds the top-up from a reader, and when nothing is short', async () => {
    const { unmount } = renderDetail({
      order: CONFIRMED,
      permissions: [P.SALES_ORDER_VIEW],
      routes: { [`GET /sales/${ORDER_ID}/allocation-status`]: ok(allocationView()) },
    })
    expect(await screen.findByText('1 line short')).toBeVisible()
    expect(
      screen.queryByRole('button', { name: 'Allocate available stock' }),
    ).not.toBeInTheDocument()
    unmount()

    renderDetail({
      order: orderDetail({ status: 'ALLOCATED' }),
      permissions: [P.SALES_ORDER_VIEW, P.SALES_ORDER_UPDATE],
      routes: {
        [`GET /sales/${ORDER_ID}/allocation-status`]: ok(
          allocationView(
            { reserved_quantity: '500.0000', shortfall_quantity: '0.0000' },
            'ALLOCATED',
          ),
        ),
      },
    })
    expect(await screen.findByText('Fully allocated')).toBeVisible()
    expect(
      screen.queryByRole('button', { name: 'Allocate available stock' }),
    ).not.toBeInTheDocument()
  })

  it("lists the order's consignments for a reader of dispatches", async () => {
    renderDetail({
      order: CONFIRMED,
      permissions: [P.SALES_ORDER_VIEW, P.DISPATCH_VIEW],
      routes: {
        [`GET /sales/${ORDER_ID}/allocation-status`]: ok(allocationView()),
        'GET /dispatch': ({ url }) => {
          // Narrowed by the server, never by filtering a page in the browser.
          expect(url.searchParams.get('sales_order_id')).toBe(ORDER_ID)
          return page([
            {
              id: 'dsp-1',
              code: 'DSP/2026-27/00007',
              dispatch_type: 'SALES',
              status: 'DISPATCHED',
              customer_id: CUSTOMER_ID,
              vendor_id: null,
              warehouse_id: WAREHOUSE_ID,
              sales_order_id: ORDER_ID,
              dispatch_date: '2026-09-10',
              expected_delivery_date: null,
              actual_delivery_date: null,
              transporter_id: null,
              courier_name: null,
              tracking_id: 'TRK-1',
              total_boxes: 1,
              total_weight_kg: '0.000',
              created_at: '2026-09-10T05:00:00Z',
            },
          ])
        },
      },
    })

    expect(await screen.findByText('DSP/2026-27/00007')).toBeVisible()
    expect(screen.getByRole('link', { name: 'Open on the dispatch register' })).toHaveAttribute(
      'href',
      `/dispatch?sales_order_id=${ORDER_ID}`,
    )
  })
})
