/**
 * The goods receipt register and the dialog that books material in.
 *
 * Two things are worth a test here, and both are about a receipt that exists
 * when the storekeeper thinks it does not.
 *
 * **Receiving is `grn.create`, posting is `grn.post`, and the button follows
 * the endpoint.** `create_goods_receipt` enforces the create right and nothing
 * else. A screen that offered "Receive against PO" on the post right as well
 * walked a poster without it through a whole count for a 403 - the trap
 * CLAUDE.md names, and the one the folder-level guard cannot see because both
 * constants are real.
 *
 * **Create-then-post is two requests, and the draft is committed after the
 * first.** When the post fails the dialog must close over the draft that now
 * exists and say where it is, because leaving it open with the counts filled
 * in invites a second submit that creates a second draft for the same lines.
 *
 * And the line errors: the payload holds only the lines a quantity was typed
 * on, renumbered from one, so a 422 for payload line 0 has to land on the form
 * line that was actually sent.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { P } from '@/lib/permissions'
import { toast } from '@/lib/toast'
import { fail, ok, page, renderScreen, type FetchRecorder, type RouteTable } from '@/test/harness'

import GoodsReceiptsPage from './goods-receipts-page'

/** Pseudo-classes jsdom answers by re-entering its own selector engine. */
const TOP_LAYER_SELECTORS = new Set([':modal', ':fullscreen', ':popover-open'])

beforeAll(() => {
  // Floating UI asks every popper whether its element is in the top layer, and
  // nwsapi answers `:modal` by asking jsdom to match `:fullscreen`, which
  // re-enters nwsapi: one popover costs tens of millions of `matches` calls and
  // roughly twenty seconds. False is what a browser answers here anyway.
  const nativeMatches = Element.prototype.matches
  Element.prototype.matches = function (this: Element, selector: string) {
    return TOP_LAYER_SELECTORS.has(selector) ? false : nativeMatches.call(this, selector)
  }
  // Radix Select reads the Pointer Capture API off its trigger and scrolls the
  // active option into view; jsdom implements neither.
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.setPointerCapture ??= () => undefined
  Element.prototype.releasePointerCapture ??= () => undefined
  Element.prototype.scrollIntoView ??= () => undefined
})

afterEach(() => {
  vi.restoreAllMocks()
})

/** Wait for something that arrives after a fetch, then read it fresh. */
async function stable<T extends HTMLElement>(read: () => T): Promise<T> {
  await waitFor(read)
  return read()
}

/**
 * Wait until the screen has stopped fetching.
 *
 * The vendor and warehouse names come from reference lookups that settle after
 * the rows do, and the commit that adds them rebuilds the row's DOM: a press
 * that began before that commit finishes on a node no longer in the document,
 * and nothing happens at all. Two polls with no new request means the rows have
 * stopped moving.
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

const RECEIPT_ID = '01a00000-0000-7000-8000-00000000grn1'
const NEW_RECEIPT_ID = '01a00000-0000-7000-8000-00000000grn2'
const PO_ID = '01a00000-0000-7000-8000-000000000po1'
const PO_LINE_A = '01a00000-0000-7000-8000-00000000pol1'
const PO_LINE_B = '01a00000-0000-7000-8000-00000000pol2'
const VENDOR_ID = '01a00000-0000-7000-8000-00000000ven1'
const WAREHOUSE_ID = '01a00000-0000-7000-8000-00000000wh01'
const UOM_ID = '01a00000-0000-7000-8000-00000000uom1'
const RECEIPTS_PATH = '/procurement/goods-receipts'
const ORDERS_PATH = '/procurement/purchase-orders'

type User = ReturnType<typeof renderScreen>['user']

const RECEIPT_ROW = {
  id: RECEIPT_ID,
  code: 'GRN-0007',
  status: 'DRAFT',
  receipt_date: '2026-09-02',
  vendor_id: VENDOR_ID,
  warehouse_id: WAREHOUSE_ID,
  purchase_order_id: PO_ID,
  vendor_delivery_note: 'DC-441',
  vendor_invoice_number: null,
  total_received_quantity: '10.0000',
  total_accepted_quantity: '10.0000',
  total_rejected_quantity: '0.0000',
  posted_at: null,
  created_at: '2026-09-02T10:00:00Z',
}

/** An order the vendor has, so material can legitimately turn up against it. */
const PO_ROW = {
  id: PO_ID,
  code: 'PO-0001',
  status: 'SENT',
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
  approved_at: '2026-09-01T10:00:00Z',
  acknowledged_at: null,
  sent_at: '2026-09-01T11:00:00Z',
  assigned_vendor_user_id: null,
  version: 2,
  created_at: '2026-09-01T09:00:00Z',
}

function poLine(overrides: Record<string, unknown> = {}) {
  return {
    id: PO_LINE_A,
    purchase_order_id: PO_ID,
    line_number: 1,
    item_id: '01a00000-0000-7000-8000-0000000item1',
    description: 'Brass rod 12mm',
    uom_id: UOM_ID,
    quantity: '10.0000',
    received_quantity: '0.0000',
    accepted_quantity: '0.0000',
    rejected_quantity: '0.0000',
    returned_quantity: '0.0000',
    unit_price: '400.000000',
    discount: '0.0000',
    discount_percentage: '0.0000',
    tax_rate: '18.0000',
    tax_amount: '720.0000',
    line_total: '4720.0000',
    required_date: null,
    requires_qc: false,
    status: 'OPEN',
    notes: null,
    quotation_line_id: null,
    ...overrides,
  }
}

function orderDetail(lines = [poLine()]) {
  return ok({ ...PO_ROW, lines })
}

function registerRoutes(rows: Record<string, unknown>[], extra: RouteTable = {}): RouteTable {
  return {
    [`GET ${RECEIPTS_PATH}`]: page(rows),
    'GET /vendors/options': ok([
      { id: VENDOR_ID, code: 'V-ACME', name: 'Acme Metals', is_approved: true },
    ]),
    'GET /warehouses/options': ok([
      { id: WAREHOUSE_ID, code: 'WH-MAIN', name: 'Main store', is_active: true },
    ]),
    'GET /products/uoms': page([{ id: UOM_ID, code: 'NOS', name: 'Numbers' }]),
    ...extra,
  }
}

/** Everything the receive dialog reaches for on top of the register. */
function formRoutes(extra: RouteTable = {}): RouteTable {
  return registerRoutes([RECEIPT_ROW], {
    [`GET ${ORDERS_PATH}`]: page([PO_ROW]),
    // The longer path wins the prefix match, so the order's lines are not
    // served the list envelope by accident.
    [`GET ${ORDERS_PATH}/${PO_ID}`]: orderDetail([
      poLine(),
      poLine({ id: PO_LINE_B, line_number: 2, description: 'Stainless sheet 2mm' }),
    ]),
    ...extra,
  })
}

const READER = [P.GRN_VIEW, P.VENDOR_VIEW, P.WAREHOUSE_VIEW, P.UOM_VIEW]
const POSTER = [...READER, P.GRN_POST]
const CLERK = [...READER, P.GRN_CREATE]
const STOREKEEPER = [...READER, P.GRN_CREATE, P.GRN_POST]

/** Open the dialog, choose the order and wait for its lines. */
async function openReceipt(user: User, fetch: FetchRecorder) {
  await settled(fetch)
  await user.click(screen.getByRole('button', { name: /receive against po/i }))
  await stable(() => screen.getByRole('dialog'))
  await user.click(screen.getByRole('combobox', { name: 'Purchase order' }))
  await user.click(await stable(() => screen.getByRole('option', { name: /PO-0001/ })))
  await stable(() => screen.getByText('Brass rod 12mm'))
}

/** Count a line as received in full: accepted must equal received. */
async function countLine(user: User, index: number, quantity: string) {
  await user.type(screen.getAllByRole('textbox', { name: 'Received' })[index]!, quantity)
  await user.type(screen.getAllByRole('textbox', { name: 'Accepted' })[index]!, quantity)
}

describe('who may receive', () => {
  it('withholds Receive against PO from grn.post alone, which the create endpoint does not accept', async () => {
    // `create_goods_receipt` enforces grn.create. Offering the dialog on the
    // post right walked a poster through a whole count for a 403.
    renderScreen(<GoodsReceiptsPage />, {
      permissions: POSTER,
      routes: registerRoutes([RECEIPT_ROW]),
    })

    await stable(() => screen.getByText('GRN-0007'))
    expect(screen.queryByRole('button', { name: /receive against po/i })).not.toBeInTheDocument()
    // The post right still gates what it enforces: posting a draft.
    expect(screen.getByRole('button', { name: 'Post' })).toBeVisible()
  })

  it('offers it on grn.create, with or without the posting right', async () => {
    renderScreen(<GoodsReceiptsPage />, {
      permissions: CLERK,
      routes: registerRoutes([RECEIPT_ROW]),
    })

    expect(
      await stable(() => screen.getByRole('button', { name: /receive against po/i })),
    ).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Post' })).not.toBeInTheDocument()
  })
})

describe('what one submit achieves', () => {
  it('closes over the draft and says so when the post fails after the create succeeded', async () => {
    const warning = vi.spyOn(toast, 'warning')
    const postPath = `${RECEIPTS_PATH}/${NEW_RECEIPT_ID}/post`
    const { user, fetch } = renderScreen(<GoodsReceiptsPage />, {
      permissions: STOREKEEPER,
      routes: formRoutes({
        [`POST ${RECEIPTS_PATH}`]: ok({ id: NEW_RECEIPT_ID, code: 'GRN-0008' }),
        [`POST ${postPath}`]: fail(
          409,
          'INVALID_STATE_TRANSITION',
          'The order is no longer receivable',
        ),
      }),
    })

    await openReceipt(user, fetch)
    await countLine(user, 0, '10')
    await user.click(screen.getByRole('button', { name: 'Post receipt' }))

    // The draft is committed the moment the create returns. What the
    // storekeeper is owed is its code and the reason it is still a draft, not
    // a dialog left open over a document that already exists.
    await waitFor(() => expect(warning).toHaveBeenCalledTimes(1))
    const [title, description] = warning.mock.calls[0]!
    expect(title).toBe('Goods receipt saved but not posted')
    expect(description).toContain('GRN-0008')
    expect(description).toContain('The order is no longer receivable')

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    // One create, one post - and never a second create for the same lines.
    expect(fetch.calls.filter((c) => c.method === 'POST' && c.path === RECEIPTS_PATH)).toHaveLength(
      1,
    )
    expect(fetch.calls.filter((c) => c.method === 'POST' && c.path === postPath)).toHaveLength(1)
    // The register is refreshed so the draft is where the toast says it is.
    await waitFor(() =>
      expect(
        fetch.calls.filter((c) => c.method === 'GET' && c.path === RECEIPTS_PATH).length,
      ).toBeGreaterThan(1),
    )
  })

  it('leaves a draft for somebody with the posting right when this account has none', async () => {
    const posted = vi.spyOn(toast, 'success')
    const { user, fetch } = renderScreen(<GoodsReceiptsPage />, {
      permissions: CLERK,
      routes: formRoutes({
        [`POST ${RECEIPTS_PATH}`]: ok({ id: NEW_RECEIPT_ID, code: 'GRN-0008' }),
      }),
    })

    await openReceipt(user, fetch)
    await countLine(user, 0, '10')
    await user.click(screen.getByRole('button', { name: 'Save draft receipt' }))

    await waitFor(() => expect(posted).toHaveBeenCalledTimes(1))
    expect(posted.mock.calls[0]![0]).toBe('Goods receipt saved')
    expect(posted.mock.calls[0]![1]).toContain('GRN-0008')
    // Creating records the count; posting is a separate right, so it is not
    // attempted on an account that does not hold it.
    expect(fetch.calls.some((c) => c.method === 'POST' && c.path.endsWith('/post'))).toBe(false)
  })

  it('sends only the counted lines, renumbered from one', async () => {
    const { user, fetch } = renderScreen(<GoodsReceiptsPage />, {
      permissions: STOREKEEPER,
      routes: formRoutes({
        [`POST ${RECEIPTS_PATH}`]: ok({ id: NEW_RECEIPT_ID, code: 'GRN-0008' }),
        [`POST ${RECEIPTS_PATH}/${NEW_RECEIPT_ID}/post`]: ok({
          receipt: { id: NEW_RECEIPT_ID, code: 'GRN-0008', status: 'POSTED' },
          movements: [],
          purchase_order_status: 'PARTIALLY_RECEIVED',
          qc_hold_line_count: 0,
          qc_inspections_created: false,
        }),
      }),
    })

    await openReceipt(user, fetch)
    await stable(() => screen.getByText('Stainless sheet 2mm'))
    // Only the second line arrived; the first is left blank.
    await countLine(user, 1, '4')
    await user.click(screen.getByRole('button', { name: 'Post receipt' }))

    await waitFor(() => fetch.requestFor('POST', RECEIPTS_PATH))
    const body = fetch.requestFor('POST', RECEIPTS_PATH) as { lines: Record<string, unknown>[] }
    expect(body.lines).toEqual([
      expect.objectContaining({
        line_number: 1,
        purchase_order_line_id: PO_LINE_B,
        received_quantity: '4',
        accepted_quantity: '4',
        rejected_quantity: '0',
      }),
    ])
  })

  it('lands a line error on the line that was actually sent, not on payload index 0', async () => {
    const { user, fetch } = renderScreen(<GoodsReceiptsPage />, {
      permissions: STOREKEEPER,
      routes: formRoutes({
        [`POST ${RECEIPTS_PATH}`]: fail(422, 'VALIDATION_ERROR', 'The receipt was refused', {
          fields: { 'lines.0.received_quantity': ['A batch number is required for this item'] },
        }),
      }),
    })

    await openReceipt(user, fetch)
    await stable(() => screen.getByText('Stainless sheet 2mm'))
    // A quantity typed only on the second line makes it payload line 0.
    await countLine(user, 1, '4')
    await user.click(screen.getByRole('button', { name: 'Post receipt' }))

    await stable(() => screen.getByText('A batch number is required for this item'))
    const received = screen.getAllByRole('textbox', { name: 'Received' })
    expect(received[1]).toHaveAttribute('aria-invalid', 'true')
    // The untouched first line is not blamed for a line it never sent.
    expect(received[0]).toHaveAttribute('aria-invalid', 'false')
    expect(screen.getByRole('dialog')).toBeVisible()
  })
})

describe('the register itself', () => {
  it('leads with whether a receipt has been posted', async () => {
    renderScreen(<GoodsReceiptsPage />, {
      permissions: READER,
      routes: registerRoutes([RECEIPT_ROW]),
    })

    // A drafted-but-unposted receipt is material in the building the system
    // does not yet own, and that gap is what this screen exists to show.
    await stable(() => screen.getByText('Not posted - no stock yet'))
    expect(screen.queryByRole('button', { name: 'Post' })).not.toBeInTheDocument()
  })

  it('reports a failed list rather than an empty one', async () => {
    renderScreen(<GoodsReceiptsPage />, {
      permissions: READER,
      routes: registerRoutes([], {
        [`GET ${RECEIPTS_PATH}`]: fail(500, 'INTERNAL', 'The receipt register is unavailable'),
      }),
    })

    await stable(() => screen.getByText('Could not load this list'))
    expect(screen.getByText('The receipt register is unavailable')).toBeVisible()
  })

  it('hides the vendor filter from an account that cannot search vendors', async () => {
    renderScreen(<GoodsReceiptsPage />, {
      permissions: [P.GRN_VIEW],
      routes: { [`GET ${RECEIPTS_PATH}`]: page([RECEIPT_ROW]) },
    })

    await stable(() => screen.getByText('GRN-0007'))
    // `/vendors` enforces vendor.view; a picker without it 403s on every key.
    expect(within(screen.getByRole('table')).getByText('GRN-0007')).toBeVisible()
    expect(screen.queryByRole('combobox', { name: 'Vendor' })).not.toBeInTheDocument()
  })
})
