/**
 * The purchase order book, the dialog that raises an order, and the detail
 * screen where an order moves through its life.
 *
 * Three things are worth a test here and they are all about money leaving.
 *
 * **The lifecycle is not a set of buttons with a permission on each.**
 * `PURCHASE_ORDER_FLOW` has no DRAFT -> APPROVED edge, so a draft offers Submit
 * and never Approve however many rights the reader holds. Offering the shortcut
 * would walk an approver through a confirmation that ends in a 422 with the
 * order exactly where it was. And APPROVED is deliberately not receivable - the
 * vendor has not seen the order, so nothing can have shipped.
 *
 * **What the form sends.** Quantities and unit prices are decimal strings the
 * whole way; `payment_terms_days` is a whole number and is *omitted* when blank
 * so the vendor's agreed terms apply, because zero days is a different
 * statement; and `line_number` is re-issued from the editor's own order.
 *
 * **The duplicate refusal is a conversation, not a toast.** A 409
 * `DUPLICATE_DOCUMENT` opens the resolution dialog, and the one resolution with
 * a create-time endpoint - resubmit with `acknowledge_duplicates` - is refused
 * by the API without `purchase_order.approve`. The dialog has to gate on that
 * same right or it moves the refusal one click later.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type FetchRecorder, type RouteTable } from '@/test/harness'

import type { PurchaseOrderRow } from './procurement-types'
import PurchaseOrderDetailPage from './purchase-order-detail-page'
import PurchaseOrdersPage from './purchase-orders-page'

/**
 * Wait for a control, then read it again.
 *
 * The vendor and warehouse lookups settle after the rows do, and the commit
 * that follows replaces the row's DOM. A node captured by `findBy*` before that
 * commit is detached by the time it is clicked, so every interaction re-reads
 * its target immediately before using it.
 */
async function stable<T extends HTMLElement>(read: () => T): Promise<T> {
  await waitFor(read)
  return read()
}

/**
 * Wait until the screen has stopped fetching.
 *
 * The vendor, warehouse, unit and tracking lookups settle after the rows and
 * the header do, and the commit that adds their names rebuilds that DOM. A
 * press that began before that commit finishes on a node no longer in the
 * document, and nothing happens at all - so every test that works a control
 * waits here first. Two polls with no new request means everything the screen
 * asked for has arrived and it has stopped moving.
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

const PO_ID = '01a00000-0000-7000-8000-000000000po1'
const VENDOR_ID = '01a00000-0000-7000-8000-00000000ven1'
const WAREHOUSE_ID = '01a00000-0000-7000-8000-00000000wh01'
const ITEM_BRASS = '01a00000-0000-7000-8000-0000000item1'
const ITEM_STEEL = '01a00000-0000-7000-8000-0000000item2'
const ORDERS_PATH = '/procurement/purchase-orders'

type User = ReturnType<typeof renderScreen>['user']

function order(overrides: Partial<PurchaseOrderRow> = {}): PurchaseOrderRow {
  return {
    id: PO_ID,
    code: 'PO-0001',
    status: 'DRAFT',
    amendment_number: 0,
    po_date: '2026-09-01',
    expected_delivery_date: '2026-09-30',
    priority: 5,
    currency: 'INR',
    subtotal: '10000.0000',
    tax_amount: '1800.0000',
    total_amount: '11800.0000',
    discount: '0.0000',
    freight_amount: '0.0000',
    payment_terms_days: 30,
    source_type: 'MANUAL',
    source_id: null,
    vendor_id: VENDOR_ID,
    warehouse_id: WAREHOUSE_ID,
    approved_at: null,
    acknowledged_at: null,
    sent_at: null,
    assigned_vendor_user_id: null,
    item_category_id: null,
    version: 1,
    created_at: '2026-09-01T09:00:00Z',
    ...overrides,
  }
}

const VENDOR_OPTIONS = ok([
  { id: VENDOR_ID, code: 'V-ACME', name: 'Acme Metals', is_approved: true },
])

function bookRoutes(rows: PurchaseOrderRow[], extra: RouteTable = {}): RouteTable {
  return {
    [`GET ${ORDERS_PATH}`]: page(rows),
    'GET /vendors/options': VENDOR_OPTIONS,
    ...extra,
  }
}

/**
 * Everything the raise-an-order dialog reaches for on top of the book.
 *
 * One row on purpose: an empty table renders a second "New purchase order"
 * button inside its empty state, and these tests are about the header one.
 */
function formRoutes(extra: RouteTable = {}): RouteTable {
  return bookRoutes([order()], {
    'GET /warehouses': page([{ id: WAREHOUSE_ID, code: 'WH-MAIN', name: 'Main store' }]),
    'GET /vendors': page([
      { id: VENDOR_ID, code: 'V-ACME', name: 'Acme Metals', is_approved: true },
    ]),
    'GET /products/items': page([
      { id: ITEM_BRASS, code: 'BRASS-ROD-12', name: 'Brass rod 12mm' },
      { id: ITEM_STEEL, code: 'SS-SHEET-2', name: 'Stainless sheet 2mm' },
    ]),
    ...extra,
  })
}

// ------------------------------------------------------------- the detail --

function detail(status: PurchaseOrderRow['status']) {
  return ok({
    ...order({ status }),
    warehouse_id: WAREHOUSE_ID,
    branch_id: null,
    delivery_address: null,
    terms: null,
    notes: null,
    vendor_reference: null,
    tracking_reference: null,
    courier_name: null,
    shipped_at: null,
    approved_by_id: null,
    rejection_reason: null,
    cancellation_reason: null,
    duplicate_acknowledged: false,
    duplicate_acknowledgement_reason: null,
    lines: [],
  })
}

const TRACKING = ok({
  purchase_order_id: PO_ID,
  code: 'PO-0001',
  status: 'DRAFT',
  vendor_id: VENDOR_ID,
  vendor_name: 'Acme Metals',
  warehouse_id: WAREHOUSE_ID,
  po_date: '2026-09-01',
  expected_delivery_date: '2026-09-30',
  sent_at: null,
  acknowledged_at: null,
  shipped_at: null,
  courier_name: null,
  tracking_reference: null,
  is_delayed: false,
  days_late: 0,
  ordered_quantity: '10.0000',
  received_quantity: '0.0000',
  accepted_quantity: '0.0000',
  rejected_quantity: '0.0000',
  pending_quantity: '10.0000',
  qc_pending: false,
  qc_pending_line_count: 0,
  grn_pending: false,
  receipts: [],
})

function detailRoutes(status: PurchaseOrderRow['status'], extra: RouteTable = {}): RouteTable {
  return {
    // The longer path wins the prefix match, so tracking is not served the
    // order payload by accident.
    [`GET ${ORDERS_PATH}/${PO_ID}/tracking`]: TRACKING,
    [`GET ${ORDERS_PATH}/${PO_ID}/shipments`]: page([]),
    [`GET ${ORDERS_PATH}/${PO_ID}/documents`]: page([]),
    [`GET ${ORDERS_PATH}`]: detail(status),
    'GET /warehouses/options': ok([
      { id: WAREHOUSE_ID, code: 'WH-MAIN', name: 'Main store', is_active: true },
    ]),
    'GET /products/uoms': page([{ id: 'u1', code: 'NOS', name: 'Numbers' }]),
    ...extra,
  }
}

function renderDetail(
  status: PurchaseOrderRow['status'],
  permissions: string[],
  extra?: RouteTable,
) {
  return renderScreen(<PurchaseOrderDetailPage />, {
    permissions,
    route: `${ORDERS_PATH}/${PO_ID}`,
    path: `${ORDERS_PATH}/:poId`,
    routes: detailRoutes(status, extra),
  })
}

describe('who may raise an order', () => {
  it('offers New purchase order only with the create right', async () => {
    renderScreen(<PurchaseOrdersPage />, {
      permissions: [P.PURCHASE_ORDER_VIEW, P.VENDOR_VIEW],
      routes: bookRoutes([order()]),
    })

    await stable(() => screen.getByText('PO-0001'))
    expect(screen.queryByRole('button', { name: /new purchase order/i })).not.toBeInTheDocument()
  })

  it('offers it once the create right is held', async () => {
    renderScreen(<PurchaseOrdersPage />, {
      permissions: [P.PURCHASE_ORDER_VIEW, P.PURCHASE_ORDER_CREATE, P.VENDOR_VIEW],
      routes: bookRoutes([order()]),
    })

    expect(
      await stable(() => screen.getByRole('button', { name: /new purchase order/i })),
    ).toBeVisible()
  })
})

describe('the order book', () => {
  it('flags an open order past its expected delivery, and only an open one', async () => {
    renderScreen(<PurchaseOrdersPage />, {
      permissions: [P.PURCHASE_ORDER_VIEW, P.VENDOR_VIEW],
      routes: bookRoutes([
        order({ id: 'a', code: 'PO-LATE', status: 'SENT', expected_delivery_date: '2020-01-01' }),
        order({ id: 'b', code: 'PO-DONE', status: 'CLOSED', expected_delivery_date: '2020-01-01' }),
      ]),
    })

    // "Open" is the API's own `OPEN_PO_STATUSES`. The row states no quantities,
    // so a receipt percentage cannot be computed from it - a closed order with
    // an old expected date is settled, not late.
    await stable(() => screen.getByText(/d late$/))
    const late = screen.getByText('PO-LATE').closest('tr')!
    const done = screen.getByText('PO-DONE').closest('tr')!
    expect(within(late).getByText(/d late$/)).toBeVisible()
    expect(within(done).queryByText(/d late$/)).not.toBeInTheDocument()
  })

  it('names the vendor from the bounded lookup, and says nothing when it is absent', async () => {
    renderScreen(<PurchaseOrdersPage />, {
      permissions: [P.PURCHASE_ORDER_VIEW, P.VENDOR_VIEW],
      routes: bookRoutes([
        order({ id: 'a', code: 'PO-KNOWN' }),
        order({ id: 'b', code: 'PO-SUSPENDED', vendor_id: 'someone-not-listed' }),
      ]),
    })

    await stable(() => screen.getByText('Acme Metals'))
    // A suspended vendor is not in the picker projection. A dash is honest here;
    // inventing a name from the id would not be.
    const suspended = screen.getByText('PO-SUSPENDED').closest('tr')!
    expect(within(suspended).queryByText('Acme Metals')).not.toBeInTheDocument()
  })

  it('reports a failed list rather than an empty one', async () => {
    renderScreen(<PurchaseOrdersPage />, {
      permissions: [P.PURCHASE_ORDER_VIEW, P.VENDOR_VIEW],
      routes: bookRoutes([], {
        [`GET ${ORDERS_PATH}`]: fail(503, 'UNAVAILABLE', 'The order service is down'),
      }),
    })

    await stable(() => screen.getByText('Could not load this list'))
    expect(screen.getByText('The order service is down', { exact: false })).toBeVisible()
  })

  it('offers the empty state its own way to raise the first order', async () => {
    renderScreen(<PurchaseOrdersPage />, {
      permissions: [P.PURCHASE_ORDER_VIEW, P.PURCHASE_ORDER_CREATE, P.VENDOR_VIEW],
      routes: bookRoutes([]),
    })

    await stable(() => screen.getByText('No purchase orders found'))
    // Two entry points, both gated on the same right: the header action and the
    // one inside the empty table.
    expect(screen.getAllByRole('button', { name: /new purchase order/i })).toHaveLength(2)
  })
})

describe('the book by item category', () => {
  const LOCKS = '01a00000-0000-7000-8000-00000000clck'
  const lockOrder = order({
    item_category_id: LOCKS,
    item_category_code: 'LOCK-MORT',
    item_category_name: 'Mortise Locks',
  })

  it('names each order’s category from the row, without the category master', async () => {
    // Buyers hold no `category.view`; the API names the category on the row,
    // so the column reads for them rather than as a column of dashes.
    const { fetch } = renderScreen(<PurchaseOrdersPage />, {
      permissions: [P.PURCHASE_ORDER_VIEW, P.VENDOR_VIEW],
      routes: bookRoutes([lockOrder, order({ id: 'b', code: 'PO-MIXED', item_category_id: null })]),
    })

    expect(
      await stable(() => screen.getByRole('button', { name: 'Show only Mortise Locks orders' })),
    ).toBeVisible()
    const mixed = screen.getByText('PO-MIXED').closest('tr')!
    expect(within(mixed).queryByRole('button', { name: /^Show only/ })).not.toBeInTheDocument()
    // No category master was asked for: the right to read it is not held.
    expect(fetch.calls.some((call) => call.path === '/products/categories')).toBe(false)
  })

  it('narrows the book to a category clicked on a row, and lets it go again', async () => {
    const asked: (string | null)[] = []
    const { user, fetch } = renderScreen(<PurchaseOrdersPage />, {
      permissions: [P.PURCHASE_ORDER_VIEW, P.VENDOR_VIEW],
      routes: bookRoutes([], {
        [`GET ${ORDERS_PATH}`]: ({ url }) => {
          asked.push(url.searchParams.get('item_category_id'))
          return page([lockOrder])
        },
      }),
    })

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: 'Show only Mortise Locks orders' }))

    // The API takes the category and everything beneath it.
    await waitFor(() => expect(asked.at(-1)).toBe(LOCKS))
    expect(await stable(() => screen.getByText('Category: Mortise Locks'))).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Remove the category filter' }))
    await waitFor(() => expect(asked.at(-1)).toBeNull())
  })

  it('offers the category master as a filter to whoever may read it', async () => {
    const { fetch } = renderScreen(<PurchaseOrdersPage />, {
      permissions: [P.PURCHASE_ORDER_VIEW, P.VENDOR_VIEW, P.CATEGORY_VIEW],
      routes: bookRoutes([lockOrder], {
        'GET /products/categories': page([
          {
            id: LOCKS,
            code: 'LOCK-MORT',
            name: 'Mortise Locks',
            path: `/${LOCKS}/`,
            depth: 0,
            is_active: true,
          },
        ]),
      }),
    })

    await settled(fetch)
    expect(screen.getByRole('combobox', { name: 'Filter by item category' })).toBeVisible()
  })
})

describe('what the order form sends', () => {
  const formPermissions = [
    P.PURCHASE_ORDER_VIEW,
    P.PURCHASE_ORDER_CREATE,
    P.VENDOR_VIEW,
    P.WAREHOUSE_VIEW,
    P.ITEM_VIEW,
  ]

  async function openForm(user: User, fetch: FetchRecorder) {
    await settled(fetch)
    await user.click(screen.getByRole('button', { name: /new purchase order/i }))
    await stable(() => screen.getByRole('dialog'))
    await user.type(screen.getByLabelText(/expected delivery/i), '2026-11-20')
    await user.click(screen.getByRole('combobox', { name: 'Vendor' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /V-ACME/ })))
    await user.click(screen.getByRole('combobox', { name: 'Deliver to' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /WH-MAIN/ })))
  }

  async function fillLine(user: User, index: number, item: RegExp, qty: string, price: string) {
    await user.click(screen.getByRole('combobox', { name: `Line ${index + 1} item` }))
    await user.click(await stable(() => screen.getByRole('option', { name: item })))
    await user.click(screen.getAllByRole('textbox', { name: 'Quantity' })[index]!)
    await user.paste(qty)
    await user.click(screen.getAllByRole('textbox', { name: 'Unit price' })[index]!)
    await user.paste(price)
  }

  it('sends two lines with the quantities and prices still decimal strings', async () => {
    const { user, fetch } = renderScreen(<PurchaseOrdersPage />, {
      permissions: formPermissions,
      routes: formRoutes({ [`POST ${ORDERS_PATH}`]: ok({ id: PO_ID, code: 'PO-0002' }) }),
    })

    await openForm(user, fetch)
    await fillLine(user, 0, /BRASS-ROD-12/, '12.5', '340.250000')
    await user.click(screen.getByRole('button', { name: /add line/i }))
    await fillLine(user, 1, /SS-SHEET-2/, '4', '1200')

    await user.click(screen.getByRole('button', { name: 'Create draft order' }))

    await waitFor(() => fetch.requestFor('POST', ORDERS_PATH))
    const body = fetch.requestFor('POST', ORDERS_PATH) as Record<string, unknown> & {
      lines: Record<string, unknown>[]
    }

    expect(body.vendor_id).toBe(VENDOR_ID)
    expect(body.warehouse_id).toBe(WAREHOUSE_ID)
    expect(body.expected_delivery_date).toBe('2026-11-20')
    // Blank terms are omitted rather than zeroed: the API then falls back to the
    // vendor's agreed terms, and "0 days" is a different instruction entirely.
    expect(body).not.toHaveProperty('payment_terms_days')
    expect(body.delivery_address).toBeNull()
    expect(body.vendor_reference).toBeNull()

    expect(body.lines).toEqual([
      {
        line_number: 1,
        item_id: ITEM_BRASS,
        quantity: '12.5',
        unit_price: '340.250000',
        requires_qc: false,
      },
      {
        line_number: 2,
        item_id: ITEM_STEEL,
        quantity: '4',
        unit_price: '1200',
        requires_qc: false,
      },
    ])
    // Six decimal places on a unit price is the contract's own precision. A
    // float would hand the API 340.25 and lose what the buyer negotiated.
    expect(typeof body.lines[0]!.unit_price).toBe('string')
  })

  it('sends payment terms as a whole number when they are given', async () => {
    const { user, fetch } = renderScreen(<PurchaseOrdersPage />, {
      permissions: formPermissions,
      routes: formRoutes({ [`POST ${ORDERS_PATH}`]: ok({ id: PO_ID, code: 'PO-0003' }) }),
    })

    await openForm(user, fetch)
    await fillLine(user, 0, /BRASS-ROD-12/, '1', '10')
    await user.type(screen.getByRole('textbox', { name: /payment terms/i }), '45')
    await user.click(screen.getByRole('button', { name: 'Create draft order' }))

    await waitFor(() => fetch.requestFor('POST', ORDERS_PATH))
    const body = fetch.requestFor('POST', ORDERS_PATH) as { payment_terms_days: unknown }
    // A whole number of days, not free text and not a decimal string: the
    // contract declares an integer and would refuse "45".
    expect(body.payment_terms_days).toBe(45)
  })

  it('renumbers the line that survives a removal', async () => {
    const { user, fetch } = renderScreen(<PurchaseOrdersPage />, {
      permissions: formPermissions,
      routes: formRoutes({ [`POST ${ORDERS_PATH}`]: ok({ id: PO_ID, code: 'PO-0004' }) }),
    })

    await openForm(user, fetch)
    await fillLine(user, 0, /BRASS-ROD-12/, '1', '10')
    await user.click(screen.getByRole('button', { name: /add line/i }))
    await fillLine(user, 1, /SS-SHEET-2/, '2', '20')
    await user.click(screen.getByRole('button', { name: 'Remove line 1' }))

    await user.click(screen.getByRole('button', { name: 'Create draft order' }))

    await waitFor(() => fetch.requestFor('POST', ORDERS_PATH))
    const body = fetch.requestFor('POST', ORDERS_PATH) as { lines: Record<string, unknown>[] }
    expect(body.lines).toEqual([
      { line_number: 1, item_id: ITEM_STEEL, quantity: '2', unit_price: '20', requires_qc: false },
    ])
  })

  it('refuses to empty the line editor', async () => {
    const { user, fetch } = renderScreen(<PurchaseOrdersPage />, {
      permissions: formPermissions,
      routes: formRoutes(),
    })

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: /new purchase order/i }))
    await stable(() => screen.getByRole('dialog'))
    // An order with no lines buys nothing, and `PurchaseOrderCreate` refuses it.
    expect(screen.getByRole('button', { name: 'Remove line 1' })).toBeDisabled()
  })

  it('lands a server field error on the line that caused it', async () => {
    const { user, fetch } = renderScreen(<PurchaseOrdersPage />, {
      permissions: formPermissions,
      routes: formRoutes({
        [`POST ${ORDERS_PATH}`]: fail(422, 'VALIDATION_ERROR', 'The order could not be raised', {
          fields: { 'lines.0.unit_price': ['Below the vendor’s agreed minimum price'] },
        }),
      }),
    })

    await openForm(user, fetch)
    await fillLine(user, 0, /BRASS-ROD-12/, '1', '0.01')
    await user.click(screen.getByRole('button', { name: 'Create draft order' }))

    await stable(() => screen.getByText('Below the vendor’s agreed minimum price'))
    // The dialog stays open with the entry intact - the buyer fixes the price
    // rather than keying the whole sheet again.
    expect(screen.getByRole('dialog')).toBeVisible()
  })

  it('keeps the form open and re-armed when the API refuses the whole request', async () => {
    const { user, fetch } = renderScreen(<PurchaseOrdersPage />, {
      permissions: formPermissions,
      routes: formRoutes({
        [`POST ${ORDERS_PATH}`]: fail(403, 'FORBIDDEN', 'You may not raise purchase orders'),
      }),
    })

    await openForm(user, fetch)
    await fillLine(user, 0, /BRASS-ROD-12/, '7', '99')
    await user.click(screen.getByRole('button', { name: 'Create draft order' }))

    // A 403 is announced by the global mutation handler, which lives on the
    // app's own query client. What the *screen* owes the buyer is everything
    // they keyed, still there, behind a button they can press again.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Create draft order' })).toBeEnabled(),
    )
    expect(screen.getByRole('dialog')).toBeVisible()
    expect(screen.getAllByRole('textbox', { name: 'Quantity' })[0]).toHaveValue('7')
  })

  it('says the order cannot be raised when the warehouse list is unreadable', async () => {
    // A purchase order has to state where the material is delivered, and that
    // is half of what the duplicate check matches on.
    const { user, fetch } = renderScreen(<PurchaseOrdersPage />, {
      permissions: [P.PURCHASE_ORDER_VIEW, P.PURCHASE_ORDER_CREATE, P.VENDOR_VIEW, P.ITEM_VIEW],
      routes: bookRoutes([order()], {
        'GET /vendors': page([]),
        'GET /products/items': page([]),
      }),
    })

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: /new purchase order/i }))
    const dialog = await stable(() => screen.getByRole('dialog'))
    expect(within(dialog).getByText(/cannot raise an order from here/i)).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Create draft order' })).toBeDisabled()
  })
})

describe('the duplicate refusal', () => {
  const duplicate = fail(409, 'DUPLICATE_DOCUMENT', 'PO-0009 already covers 40 of this material', {
    candidate_count: 1,
    candidates: [
      {
        purchase_order_id: '01a00000-0000-7000-8000-000000000po9',
        purchase_order_code: 'PO-0009',
        purchase_order_line_id: '01a00000-0000-7000-8000-00000000ln09',
        item_id: ITEM_BRASS,
        status: 'SENT',
        open_quantity: '40.0000',
        required_date: '2026-10-01',
        confidence: 'EXACT',
        match_reasons: ['Same item, same warehouse, within 14 days'],
        recommended_action: 'CREATE_NEW_WITH_APPROVAL',
        available_actions: ['CREATE_NEW_WITH_APPROVAL', 'MERGE_WITH_EXISTING'],
      },
    ],
  })

  const duplicatePermissions = [
    P.PURCHASE_ORDER_VIEW,
    P.PURCHASE_ORDER_CREATE,
    P.VENDOR_VIEW,
    P.WAREHOUSE_VIEW,
    P.ITEM_VIEW,
  ]

  async function raiseADuplicate(user: User, fetch: FetchRecorder) {
    await settled(fetch)
    await user.click(screen.getByRole('button', { name: /new purchase order/i }))
    await stable(() => screen.getByRole('dialog'))
    await user.type(screen.getByLabelText(/expected delivery/i), '2026-11-20')
    await user.click(screen.getByRole('combobox', { name: 'Vendor' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /V-ACME/ })))
    await user.click(screen.getByRole('combobox', { name: 'Deliver to' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /WH-MAIN/ })))
    await user.click(screen.getByRole('combobox', { name: 'Line 1 item' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /BRASS-ROD-12/ })))
    await user.type(screen.getAllByRole('textbox', { name: 'Quantity' })[0]!, '40')
    await user.type(screen.getAllByRole('textbox', { name: 'Unit price' })[0]!, '340')
    await user.click(screen.getByRole('button', { name: 'Create draft order' }))
  }

  it('shows which order already covers the requirement instead of a bare failure', async () => {
    const { user, fetch } = renderScreen(<PurchaseOrdersPage />, {
      permissions: duplicatePermissions,
      routes: formRoutes({ [`POST ${ORDERS_PATH}`]: duplicate }),
    })

    await raiseADuplicate(user, fetch)

    // The buyer needs the order code and the matcher's reason to judge the
    // overlap. A toast saying "duplicate" leaves them nothing to act on.
    await stable(() => screen.getByText('PO-0009 already covers 40 of this material'))
    expect(screen.getByText('PO-0009')).toBeVisible()
    expect(screen.getByText('Same item, same warehouse, within 14 days')).toBeVisible()
  })

  it('allows a buyer to request a separate order with independent approval', async () => {
    const { user, fetch } = renderScreen(<PurchaseOrdersPage />, {
      permissions: duplicatePermissions,
      routes: formRoutes({ [`POST ${ORDERS_PATH}`]: duplicate }),
    })
    await raiseADuplicate(user, fetch)
    await stable(() => screen.getByText('PO-0009 already covers 40 of this material'))
    expect(
      screen.getByRole('button', { name: 'Create separate PO (needs approval)' }),
    ).toBeVisible()
    expect(screen.queryByRole('button', { name: /merge into/i })).not.toBeInTheDocument()
  })

  it('sends the refused draft and reason to the real separate-order action without approval rights', async () => {
    const bodies: Record<string, unknown>[] = []
    const resolutions: Record<string, unknown>[] = []
    const { user, fetch } = renderScreen(<PurchaseOrdersPage />, {
      permissions: duplicatePermissions,
      routes: formRoutes({
        [`POST ${ORDERS_PATH}`]: ({ body }) => {
          bodies.push(body as Record<string, unknown>)
          return duplicate
        },
        [`POST ${ORDERS_PATH}/duplicate-resolution`]: ({ body }) => {
          resolutions.push(body as Record<string, unknown>)
          return ok({
            action: 'CREATE_WITH_APPROVAL',
            purchase_orders: [order({ code: 'PO-0010' })],
            cancelled_requisition_line_ids: [],
            discarded: false,
          })
        },
      }),
    })
    await raiseADuplicate(user, fetch)
    await stable(() => screen.getByText('PO-0009 already covers 40 of this material'))
    await user.type(
      screen.getByRole('textbox', { name: 'Reason for this action' }),
      'Second press line commissioned this week',
    )
    await user.click(screen.getByRole('button', { name: 'Create separate PO (needs approval)' }))
    await waitFor(() => expect(resolutions).toHaveLength(1))
    expect(bodies).toHaveLength(1)
    expect(bodies[0]).toHaveProperty('acknowledge_duplicates', false)
    expect(resolutions[0]).toEqual({
      action: 'CREATE_WITH_APPROVAL',
      reason: 'Second press line commissioned this week',
      draft: { kind: 'MANUAL', purchase_order: bodies[0] },
    })
  })
})

describe('the lifecycle on the order’s own screen', () => {
  const everyRight = [
    P.PURCHASE_ORDER_VIEW,
    P.PURCHASE_ORDER_UPDATE,
    P.PURCHASE_ORDER_APPROVE,
    P.PURCHASE_ORDER_REJECT,
    P.PURCHASE_ORDER_SEND,
    P.PURCHASE_ORDER_CANCEL,
  ]

  it('offers Submit on a draft and never Approve, however many rights are held', async () => {
    // `PURCHASE_ORDER_FLOW` has no DRAFT -> APPROVED edge. Offering the shortcut
    // walks an approver through a confirmation that ends in a 422 with the order
    // exactly where it started.
    renderDetail('DRAFT', everyRight)

    await stable(() => screen.getByRole('button', { name: /submit for approval/i }))
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reject' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /send to vendor/i })).not.toBeInTheDocument()
  })

  it('offers the decision only once the order is awaiting one', async () => {
    renderDetail('PENDING_APPROVAL', everyRight)

    await stable(() => screen.getByRole('button', { name: 'Approve' }))
    expect(screen.getByRole('button', { name: 'Reject' })).toBeVisible()
    // A document somebody is deciding on cannot be resubmitted underneath them.
    expect(screen.queryByRole('button', { name: /submit for approval/i })).not.toBeInTheDocument()
  })

  it('separates approval from rejection, which are two rights and two people', async () => {
    renderDetail('PENDING_APPROVAL', [P.PURCHASE_ORDER_VIEW, P.PURCHASE_ORDER_REJECT])

    await stable(() => screen.getByRole('button', { name: 'Reject' }))
    // `purchase_order.reject` is not `purchase_order.approve`; the API declares
    // them on separate routes and so does the screen.
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
  })

  it('will not send an order nobody has approved', async () => {
    renderDetail('PENDING_APPROVAL', everyRight)

    await stable(() => screen.getByRole('button', { name: 'Approve' }))
    expect(screen.queryByRole('button', { name: /send to vendor/i })).not.toBeInTheDocument()
  })

  it('stops offering cancellation once material has been received', async () => {
    // Cancellation is legal only before anything has arrived; past that the
    // order is settled by closing it, not by pretending it never happened.
    renderDetail('PARTIALLY_RECEIVED', everyRight)

    // The code is both the page title and the last breadcrumb, so the heading is
    // what identifies the loaded order unambiguously.
    await stable(() => screen.getByRole('heading', { name: 'PO-0001' }))
    expect(screen.queryByRole('button', { name: /cancel order/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Short-close order' })).toBeVisible()
  })

  it.each([
    ['GRN_COMPLETED', 'Close order'],
    ['PAID', 'Close order'],
  ] as const)('offers %s close only to buyers with the cancel right', async (status, label) => {
    renderDetail(status, everyRight)

    expect(await stable(() => screen.getByRole('button', { name: label }))).toBeVisible()
  })

  it('hides Close without the purchase-order cancel right', async () => {
    renderDetail('GRN_COMPLETED', [P.PURCHASE_ORDER_VIEW])

    await stable(() => screen.getByRole('heading', { name: 'PO-0001' }))
    expect(screen.queryByRole('button', { name: 'Close order' })).not.toBeInTheDocument()
  })

  it('short-closes a partially received order with the required reason', async () => {
    const closePath = `${ORDERS_PATH}/${PO_ID}/close`
    const { user, fetch } = renderDetail('PARTIALLY_RECEIVED', everyRight, {
      [`POST ${closePath}`]: ok(order({ status: 'CLOSED' })),
    })

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: 'Short-close order' }))
    expect(await stable(() => screen.getByText('Short-close PO-0001?'))).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Short-close order' }))
    await stable(() => screen.getByText(/at least three characters/i))
    expect(fetch.calls.some((call) => call.path === closePath)).toBe(false)

    await user.type(screen.getByRole('textbox', { name: 'Reason' }), 'Vendor confirmed no balance')
    await user.click(screen.getByRole('button', { name: 'Short-close order' }))

    await waitFor(() => fetch.requestFor('POST', closePath))
    expect(fetch.requestFor('POST', closePath)).toEqual({ reason: 'Vendor confirmed no balance' })
  })

  it('posts the transition with a null reason when no note was recorded', async () => {
    const submitPath = `${ORDERS_PATH}/${PO_ID}/submit`
    const { user, fetch } = renderDetail('DRAFT', everyRight, {
      [`POST ${submitPath}`]: ok(order({ status: 'PENDING_APPROVAL' })),
    })

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: /submit for approval/i }))
    await user.click(await stable(() => screen.getByRole('button', { name: 'Submit' })))

    await waitFor(() => fetch.requestFor('POST', submitPath))
    // Null is "no note was recorded"; an empty string is a note that says
    // nothing, and the audit trail keeps whichever it is given.
    expect(fetch.requestFor('POST', submitPath)).toEqual({ reason: null })
  })

  it('will not reject an order without telling the buyer why', async () => {
    const { user, fetch } = renderDetail('PENDING_APPROVAL', everyRight)

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: 'Reject' }))
    await user.click(await stable(() => screen.getByRole('button', { name: 'Reject order' })))

    // The buyer sees this sentence and nothing else about the refusal.
    await stable(() => screen.getByText(/at least three characters/i))
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('stops the reason at the 255 characters the API accepts', async () => {
    // `ReasonRequest` and `ApproveRequest` both cap the reason at 255. Past
    // that the API answers a 422 naming the field, which the global toast
    // suppresses on the assumption the form shows it - so the control has to
    // stop the paste where it is typed.
    const { user, fetch } = renderDetail('PENDING_APPROVAL', everyRight)

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: 'Reject' }))
    const reasonBox = await stable(() => screen.getByRole('textbox', { name: 'Reason' }))
    expect(reasonBox).toHaveAttribute('maxlength', '255')
  })

  it('does not offer Send without the send right', async () => {
    renderDetail('APPROVED', [P.PURCHASE_ORDER_VIEW, P.PURCHASE_ORDER_UPDATE])

    await stable(() => screen.getByRole('heading', { name: 'PO-0001' }))
    expect(screen.queryByRole('button', { name: /send to vendor/i })).not.toBeInTheDocument()
  })

  it('says the vendor is not available when tracking fails, rather than loading forever', async () => {
    // The vendor's name comes from the tracking request alone. Its failure is
    // reported by the panel; the header must not keep promising a name that
    // is never coming.
    renderDetail('DRAFT', [P.PURCHASE_ORDER_VIEW], {
      [`GET ${ORDERS_PATH}/${PO_ID}/tracking`]: fail(500, 'INTERNAL', 'Tracking is unavailable'),
    })

    await stable(() => screen.getByRole('heading', { name: 'PO-0001' }))
    await stable(() => screen.getByText('Could not load tracking'))
    expect(screen.getByRole('link', { name: 'Not available' })).toBeVisible()
    expect(screen.queryByText('Loading...')).not.toBeInTheDocument()
  })
})

describe('sending an order names who owns it at the vendor', () => {
  const SEND_PATH = `${ORDERS_PATH}/${PO_ID}/send`
  const ASSIGNEES_PATH = `${ORDERS_PATH}/${PO_ID}/vendor-assignees`
  const senderRights = [P.PURCHASE_ORDER_VIEW, P.PURCHASE_ORDER_SEND]

  const MANAGER = {
    id: '01a00000-0000-7000-8000-00000000mgr1',
    full_name: 'Sandeep Patil',
    email: 'lockbody.manager@precisionfasteners.example',
    allowed_category_ids: ['01a00000-0000-7000-8000-00000000clck'],
    allowed_category_codes: ['LOCK-MORT'],
    is_suggested: true,
  }
  const OWNER = {
    id: '01a00000-0000-7000-8000-00000000own1',
    full_name: 'Precision Owner',
    email: 'owner@precisionfasteners.example',
    allowed_category_ids: [],
    allowed_category_codes: [],
    is_suggested: false,
  }

  /** Open the Send dialog on an approved order and wait for the picker. */
  async function openSend(user: User, fetch: FetchRecorder): Promise<HTMLElement> {
    await settled(fetch)
    await user.click(screen.getByRole('button', { name: /send to vendor/i }))
    const dialog = await stable(() => screen.getByRole('dialog'))
    await stable(() => within(dialog).getByRole('combobox', { name: 'Vendor user responsible' }))
    return dialog
  }

  it('preselects the manager the rule suggests, and sends them as the assignee', async () => {
    const { user, fetch } = renderDetail('APPROVED', senderRights, {
      [`GET ${ASSIGNEES_PATH}`]: ok([MANAGER, OWNER]),
      [`POST ${SEND_PATH}`]: ok(order({ status: 'SENT' })),
    })

    const dialog = await openSend(user, fetch)
    expect(
      within(dialog).getByRole('combobox', { name: 'Vendor user responsible' }),
    ).toHaveTextContent('Sandeep Patil (suggested)')
    // Their scope is shown, so the buyer can see why they were chosen.
    expect(within(dialog).getByText(/LOCK-MORT/)).toBeVisible()
    await user.click(within(dialog).getByRole('button', { name: 'Send order' }))

    await waitFor(() => fetch.requestFor('POST', SEND_PATH))
    expect(fetch.requestFor('POST', SEND_PATH)).toEqual({
      assigned_vendor_user_id: MANAGER.id,
      reason: null,
    })
  })

  it('sends the person the buyer picks instead, with the note', async () => {
    const { user, fetch } = renderDetail('APPROVED', senderRights, {
      [`GET ${ASSIGNEES_PATH}`]: ok([MANAGER, OWNER]),
      [`POST ${SEND_PATH}`]: ok(order({ status: 'SENT' })),
    })

    const dialog = await openSend(user, fetch)
    await user.click(within(dialog).getByRole('combobox', { name: 'Vendor user responsible' }))
    await user.click(await stable(() => screen.getByRole('option', { name: 'Precision Owner' })))
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Note' }),
      'Owner handles rush jobs',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Send order' }))

    await waitFor(() => fetch.requestFor('POST', SEND_PATH))
    expect(fetch.requestFor('POST', SEND_PATH)).toEqual({
      assigned_vendor_user_id: OWNER.id,
      reason: 'Owner handles rush jobs',
    })
  })

  it('leaves an ambiguous order unassigned unless somebody is chosen', async () => {
    // Two managers cover the category and the rule will not guess between them;
    // sending `null` is exactly "nobody", and the dialog says why.
    const { user, fetch } = renderDetail('APPROVED', senderRights, {
      [`GET ${ASSIGNEES_PATH}`]: ok([
        { ...MANAGER, is_suggested: false },
        { ...OWNER, allowed_category_codes: ['LOCK-MORT'], is_suggested: false },
      ]),
      [`POST ${SEND_PATH}`]: ok(order({ status: 'SENT' })),
    })

    const dialog = await openSend(user, fetch)
    expect(
      within(dialog).getByRole('combobox', { name: 'Vendor user responsible' }),
    ).toHaveTextContent('Leave it unassigned')
    expect(
      within(dialog).getByText(/several people at the vendor cover this category/i),
    ).toBeVisible()
    await user.click(within(dialog).getByRole('button', { name: 'Send order' }))

    await waitFor(() => fetch.requestFor('POST', SEND_PATH))
    expect(fetch.requestFor('POST', SEND_PATH)).toEqual({
      assigned_vendor_user_id: null,
      reason: null,
    })
  })

  it('names the order’s category and its vendor assignee', async () => {
    renderDetail('SENT', [P.PURCHASE_ORDER_VIEW], {
      [`GET ${ORDERS_PATH}`]: ok({
        ...(detail('SENT').data as Record<string, unknown>),
        item_category_id: '01a00000-0000-7000-8000-00000000clck',
        item_category_code: 'LOCK-MORT',
        item_category_name: 'Mortise Locks',
        assigned_vendor_user_id: MANAGER.id,
        assigned_vendor_user_name: 'Sandeep Patil',
      }),
    })

    await stable(() => screen.getByText('Mortise Locks'))
    expect(screen.getByText('LOCK-MORT')).toBeVisible()
    expect(screen.getByText('Sandeep Patil')).toBeVisible()
  })

  it('says an unsent order’s assignee is chosen when it is sent', async () => {
    renderDetail('APPROVED', [P.PURCHASE_ORDER_VIEW])

    await stable(() => screen.getByText('Chosen when the order is sent'))
    expect(screen.getByText('Several or none')).toBeVisible()
  })
})
