/**
 * The purchase return register, and the two dialogs that end a return.
 *
 * The fact this screen exists to state is whether the goods have actually left.
 * A posted goods receipt cannot be retracted - an append-only ledger is
 * corrected by an offsetting entry - so a return is the only way material that
 * was booked in comes back out, and a draft is stock the vendor thinks is coming
 * back while it is still on the shelf. POSTED and CANCELLED are therefore never
 * collapsed into one "closed" state, and the tests below hold those three
 * sentences apart.
 *
 * Two API facts drive the rest. Raising, posting and cancelling are all
 * `purchase_return.create` - posting is *not* split off behind its own right the
 * way a goods receipt's is - so gating the Post button on anything else would
 * hide it from people the API will happily let post. And `POST
 * /purchase-returns/{id}/post` takes no body at all: the reason on the document
 * is what every movement it writes is stamped with, so a body here would be an
 * invention.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { beforeAll, describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import {
  fail,
  ok,
  page,
  renderScreen,
  type FetchRecorder,
  type RouteTable,
} from '@/test/harness'

import type { PurchaseReturnRow } from './purchase-return-model'
import PurchaseReturnsPage from './purchase-returns-page'

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

const RETURN_ID = '01a00000-0000-7000-8000-00000000ret1'
const RECEIPT_ID = '01a00000-0000-7000-8000-00000000grn1'
const RECEIPT_LINE_A = '01a00000-0000-7000-8000-000000grnl1'
const RECEIPT_LINE_B = '01a00000-0000-7000-8000-000000grnl2'
const VENDOR_ID = '01a00000-0000-7000-8000-00000000ven1'
const WAREHOUSE_ID = '01a00000-0000-7000-8000-00000000wh01'
const UOM_ID = '01a00000-0000-7000-8000-00000000uom1'
const RETURNS_PATH = '/procurement/purchase-returns'
const RETURN_DETAIL_PATH = `${RETURNS_PATH}/${RETURN_ID}`

type User = ReturnType<typeof renderScreen>['user']

function purchaseReturn(overrides: Partial<PurchaseReturnRow> = {}): PurchaseReturnRow {
  return {
    id: RETURN_ID,
    code: 'PRET-0001',
    status: 'DRAFT',
    return_date: '2026-09-08',
    reason: 'Damaged in transit',
    debit_note_number: null,
    goods_receipt_id: RECEIPT_ID,
    purchase_order_id: '01a00000-0000-7000-8000-000000000po1',
    vendor_id: VENDOR_ID,
    warehouse_id: WAREHOUSE_ID,
    currency: 'INR',
    subtotal: '4000.0000',
    tax_amount: '720.0000',
    total_amount: '4720.0000',
    posted_at: null,
    created_at: '2026-09-08T09:00:00Z',
    ...overrides,
  }
}

/** The return as its detail endpoint returns it, with the lines posting needs. */
function returnDetail(overrides: Record<string, unknown> = {}, lines = 1) {
  return ok({
    ...purchaseReturn(overrides as Partial<PurchaseReturnRow>),
    notes: null,
    cancellation_reason: null,
    posted_by_id: null,
    lines: Array.from({ length: lines }, (_, index) => ({
      id: `01a00000-0000-7000-8000-00000000li0${index + 1}`,
      line_number: index + 1,
      goods_receipt_line_id: index === 0 ? RECEIPT_LINE_A : RECEIPT_LINE_B,
      item_id: '01a00000-0000-7000-8000-0000000item1',
      description: 'Brass rod 12mm',
      quantity: '10.0000',
      uom_id: UOM_ID,
      unit_cost: '400.000000',
      line_total: '4000.0000',
      tax_amount: '720.0000',
      batch_id: null,
      reason: null,
      notes: null,
    })),
    ...overrides,
  })
}

function registerRoutes(rows: PurchaseReturnRow[], extra: RouteTable = {}): RouteTable {
  return {
    [`GET ${RETURNS_PATH}`]: page(rows),
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

const RECEIPT_ROW = {
  id: RECEIPT_ID,
  code: 'GRN-0007',
  status: 'POSTED',
  receipt_date: '2026-09-02',
  vendor_id: VENDOR_ID,
  warehouse_id: WAREHOUSE_ID,
  purchase_order_id: '01a00000-0000-7000-8000-000000000po1',
  vendor_delivery_note: 'DC-441',
  vendor_invoice_number: 'INV-9002',
  total_received_quantity: '100.0000',
  total_accepted_quantity: '95.0000',
  total_rejected_quantity: '5.0000',
  posted_at: '2026-09-02T11:00:00Z',
  created_at: '2026-09-02T10:00:00Z',
}

/** A posted receipt with one returnable line and one already fully returned. */
function receiptDetail(lines = [receiptLine(), fullyReturnedLine()]) {
  return ok({ ...RECEIPT_ROW, lines })
}

function receiptLine(overrides: Record<string, unknown> = {}) {
  return {
    id: RECEIPT_LINE_A,
    goods_receipt_id: RECEIPT_ID,
    line_number: 1,
    purchase_order_line_id: '01a00000-0000-7000-8000-00000000pol1',
    item_id: '01a00000-0000-7000-8000-0000000item1',
    description: 'Brass rod 12mm',
    uom_id: UOM_ID,
    received_quantity: '50.0000',
    accepted_quantity: '40.0000',
    rejected_quantity: '10.0000',
    returned_quantity: '0.0000',
    unit_cost: '400.000000',
    batch_id: null,
    batch_number: 'LOT-88',
    expires_on: null,
    manufactured_on: null,
    location_id: null,
    qc_status: 'NOT_REQUIRED',
    qc_inspection_id: null,
    requires_qc: false,
    rejection_reason: null,
    posted_stock_state: null,
    notes: null,
    ...overrides,
  }
}

function fullyReturnedLine() {
  return receiptLine({
    id: RECEIPT_LINE_B,
    line_number: 2,
    description: 'Stainless sheet 2mm',
    accepted_quantity: '20.0000',
    returned_quantity: '20.0000',
    batch_number: null,
  })
}

const READER = [P.PURCHASE_RETURN_VIEW, P.VENDOR_VIEW, P.WAREHOUSE_VIEW]
const STOREKEEPER = [...READER, P.PURCHASE_RETURN_CREATE, P.GRN_VIEW, P.UOM_VIEW]

describe('who may send material back', () => {
  it('shows the register but no commands without purchase_return.create', async () => {
    renderScreen(<PurchaseReturnsPage />, {
      permissions: READER,
      routes: registerRoutes([purchaseReturn()]),
    })

    await stable(() => screen.getByRole('button', { name: 'PRET-0001' }))
    expect(screen.queryByRole('button', { name: /new return/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Post' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Cancel PRET-0001' })).not.toBeInTheDocument()
  })

  it('gives posting to the same right that raises the return', async () => {
    // The API declares `purchase_return.create` on all three routes - posting a
    // return is not behind a separate posting right the way `grn.post` is.
    // Gating Post on anything else hides it from people the API will let post.
    renderScreen(<PurchaseReturnsPage />, {
      permissions: STOREKEEPER,
      routes: registerRoutes([purchaseReturn()]),
    })

    expect(await stable(() => screen.getByRole('button', { name: 'Post' }))).toBeVisible()
    expect(screen.getByRole('button', { name: 'Cancel PRET-0001' })).toBeVisible()
    expect(screen.getByRole('button', { name: /new return/i })).toBeVisible()
  })
})

describe('the two ways a return ends are not the same thing', () => {
  it('offers post and cancel only while the return is still a draft', async () => {
    renderScreen(<PurchaseReturnsPage />, {
      permissions: STOREKEEPER,
      routes: registerRoutes([purchaseReturn({ status: 'DRAFT' })]),
    })

    await stable(() => screen.getByRole('button', { name: 'Post' }))
    expect(screen.getByText('Not posted - still in stock')).toBeVisible()
  })

  it('offers nothing on a posted return, which is irreversible', async () => {
    renderScreen(<PurchaseReturnsPage />, {
      permissions: STOREKEEPER,
      routes: registerRoutes([
        purchaseReturn({ status: 'POSTED', posted_at: '2026-09-09T10:00:00Z' }),
      ]),
    })

    // POSTED is terminal: the stock left and the receipt and order lines were
    // written back. There is no undo, so there is no button.
    await stable(() => screen.getByText(/^Left /))
    expect(screen.queryByRole('button', { name: 'Post' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Cancel PRET-0001' })).not.toBeInTheDocument()
  })

  it('says a cancelled return moved nothing, rather than calling it closed', async () => {
    renderScreen(<PurchaseReturnsPage />, {
      permissions: STOREKEEPER,
      routes: registerRoutes([purchaseReturn({ status: 'CANCELLED' })]),
    })

    // Showing POSTED and CANCELLED as one "closed" state would tell a
    // storekeeper a shelf is empty when the material is still on it.
    await stable(() => screen.getByText('Abandoned - nothing moved'))
    expect(screen.queryByRole('button', { name: 'Post' })).not.toBeInTheDocument()
  })
})

describe('posting the return', () => {
  it('posts with no body at all, because the endpoint takes none', async () => {
    const postPath = `${RETURN_DETAIL_PATH}/post`
    const { user, fetch } = renderScreen(<PurchaseReturnsPage />, {
      permissions: STOREKEEPER,
      routes: registerRoutes([purchaseReturn()], {
        [`GET ${RETURN_DETAIL_PATH}`]: returnDetail(),
        [`POST ${postPath}`]: ok({
          purchase_return: purchaseReturn({ status: 'POSTED' }),
          movements: [{ id: 'm1' }],
        }),
      }),
    })

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: 'Post' }))
    await user.click(
      await stable(() => screen.getByRole('button', { name: /post and move the stock/i })),
    )

    await waitFor(() => fetch.requestFor('POST', postPath))
    // The reason on the document is what every movement it writes is stamped
    // with; a body here would be this screen's invention and a 422.
    expect(fetch.requestFor('POST', postPath)).toBeUndefined()
  })

  it('states what posting will do before it is done', async () => {
    const { user, fetch } = renderScreen(<PurchaseReturnsPage />, {
      permissions: STOREKEEPER,
      routes: registerRoutes([purchaseReturn()], {
        [`GET ${RETURN_DETAIL_PATH}`]: returnDetail({}, 2),
      }),
    })

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: 'Post' }))
    const dialog = await stable(() => screen.getByRole('dialog'))

    // The lines are fetched rather than summarised from the row, because the
    // summary carries no line count - and "two lines out of WH-MAIN" is the
    // sentence that lets somebody notice this is the wrong document.
    expect(within(dialog).getByText(/posting cannot be undone/i)).toBeVisible()
    await stable(() => within(dialog).getByText(/all 2 line\(s\)/i))
    expect(within(dialog).getByText(/WH-MAIN - Main store/)).toBeVisible()
  })

  it('will not offer to post a return with no lines', async () => {
    const { user, fetch } = renderScreen(<PurchaseReturnsPage />, {
      permissions: STOREKEEPER,
      routes: registerRoutes([purchaseReturn()], {
        [`GET ${RETURN_DETAIL_PATH}`]: returnDetail({}, 0),
      }),
    })

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: 'Post' }))
    const dialog = await stable(() => screen.getByRole('dialog'))

    // The API refuses a posting without lines outright, so the button is
    // disabled rather than offering a certain failure.
    await stable(() => within(dialog).getByText('This return has no lines'))
    expect(within(dialog).getByRole('button', { name: /post and move the stock/i })).toBeDisabled()
  })
})

describe('cancelling the draft', () => {
  it('will not cancel without the reason the API stores', async () => {
    const { user, fetch } = renderScreen(<PurchaseReturnsPage />, {
      permissions: STOREKEEPER,
      routes: registerRoutes([purchaseReturn()]),
    })

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: 'Cancel PRET-0001' }))
    await user.click(await stable(() => screen.getByRole('button', { name: 'Cancel return' })))

    await stable(() => screen.getByText(/record at least 3 characters of reason/i))
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('posts the reason and nothing else', async () => {
    const cancelPath = `${RETURN_DETAIL_PATH}/cancel`
    const { user, fetch } = renderScreen(<PurchaseReturnsPage />, {
      permissions: STOREKEEPER,
      routes: registerRoutes([purchaseReturn()], {
        [`POST ${cancelPath}`]: ok(purchaseReturn({ status: 'CANCELLED' })),
      }),
    })

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: 'Cancel PRET-0001' }))
    await user.type(
      await stable(() => screen.getByRole('textbox', { name: 'Reason' })),
      '  Vendor agreed a credit instead  ',
    )
    await user.click(screen.getByRole('button', { name: 'Cancel return' }))

    await waitFor(() => fetch.requestFor('POST', cancelPath))
    expect(fetch.requestFor('POST', cancelPath)).toEqual({
      reason: 'Vendor agreed a credit instead',
    })
  })
})

describe('what the return form sends', () => {
  function formRoutes(extra: RouteTable = {}): RouteTable {
    return registerRoutes([purchaseReturn()], {
      'GET /procurement/goods-receipts': page([RECEIPT_ROW]),
      [`GET /procurement/goods-receipts/${RECEIPT_ID}`]: receiptDetail(),
      ...extra,
    })
  }

  async function openForm(user: User, fetch: FetchRecorder) {
    await settled(fetch)
    await user.click(screen.getByRole('button', { name: /new return/i }))
    await stable(() => screen.getByRole('dialog'))
    await user.click(screen.getByRole('combobox', { name: 'Goods receipt' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /GRN-0007/ })))
  }

  it('offers only the lines that still have something to send back', async () => {
    const { user, fetch } = renderScreen(<PurchaseReturnsPage />, {
      permissions: STOREKEEPER,
      routes: formRoutes(),
    })

    await openForm(user, fetch)

    // Accepted less already returned is what `create_purchase_return` bounds
    // against. A line that has gone back in full would only invite a second
    // attempt the API refuses, and a rejected quantity never became stock.
    await stable(() => screen.getByText('Brass rod 12mm'))
    expect(screen.queryByText('Stainless sheet 2mm')).not.toBeInTheDocument()
    // 40 accepted, none returned - not the 50 that were received.
    expect(screen.getByText(/Returnable 40 NOS/)).toBeVisible()
    expect(screen.getByText('Lot LOT-88')).toBeVisible()
  })

  it('sends only the lines with a quantity, renumbered from one', async () => {
    const { user, fetch } = renderScreen(<PurchaseReturnsPage />, {
      permissions: STOREKEEPER,
      routes: formRoutes({
        // Two returnable lines this time, so the numbering has something to do.
        [`GET /procurement/goods-receipts/${RECEIPT_ID}`]: receiptDetail([
          receiptLine(),
          receiptLine({
            id: RECEIPT_LINE_B,
            line_number: 2,
            description: 'Stainless sheet 2mm',
            batch_number: null,
          }),
        ]),
        [`POST ${RETURNS_PATH}`]: ok({ code: 'PRET-0002' }),
      }),
    })

    await openForm(user, fetch)
    await stable(() => screen.getByText('Stainless sheet 2mm'))

    // Only the second line is being returned; the first is left blank.
    await user.type(screen.getAllByRole('textbox', { name: 'Returning' })[1]!, '12.5')
    await user.type(screen.getByRole('textbox', { name: 'Reason' }), '  Wrong grade supplied  ')
    await user.click(screen.getByRole('button', { name: 'Raise draft return' }))

    await waitFor(() => fetch.requestFor('POST', RETURNS_PATH))
    const body = fetch.requestFor('POST', RETURNS_PATH) as Record<string, unknown> & {
      lines: Record<string, unknown>[]
    }

    expect(body.goods_receipt_id).toBe(RECEIPT_ID)
    expect(body.reason).toBe('Wrong grade supplied')
    // Blank optional text is null, never "": the debit note is the storekeeper's
    // own reference and an empty string is not one.
    expect(body.debit_note_number).toBeNull()
    expect(body.notes).toBeNull()

    // Line numbers are re-issued 1..n over the lines actually going back,
    // because the receipt's own numbering has gaps once the untouched lines are
    // dropped.
    expect(body.lines).toEqual([
      {
        line_number: 1,
        goods_receipt_line_id: RECEIPT_LINE_B,
        quantity: '12.5',
        reason: null,
        notes: null,
      },
    ])
    expect(typeof body.lines[0]!.quantity).toBe('string')
  })

  it('refuses to send back more than the receipt still has returnable', async () => {
    const { user, fetch } = renderScreen(<PurchaseReturnsPage />, {
      permissions: STOREKEEPER,
      routes: formRoutes(),
    })

    await openForm(user, fetch)
    await stable(() => screen.getByText('Brass rod 12mm'))

    await user.type(screen.getByRole('textbox', { name: 'Returning' }), '41')
    await user.type(screen.getByRole('textbox', { name: 'Reason' }), 'Damaged in transit')
    await user.click(screen.getByRole('button', { name: 'Raise draft return' }))

    // Named on the failing line rather than left to a 422 that refuses the whole
    // document without saying which line was wrong.
    await stable(() => screen.getByText(/only 40 of this line is still returnable/i))
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('refuses a return that sends nothing back at all', async () => {
    const { user, fetch } = renderScreen(<PurchaseReturnsPage />, {
      permissions: STOREKEEPER,
      routes: formRoutes(),
    })

    await openForm(user, fetch)
    await stable(() => screen.getByText('Brass rod 12mm'))

    await user.type(screen.getByRole('textbox', { name: 'Reason' }), 'Damaged in transit')
    await user.click(screen.getByRole('button', { name: 'Raise draft return' }))

    await stable(() => screen.getByText('Enter a quantity on at least one line'))
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('lands a server field error on the line that caused it', async () => {
    const { user, fetch } = renderScreen(<PurchaseReturnsPage />, {
      permissions: STOREKEEPER,
      routes: formRoutes({
        [`POST ${RETURNS_PATH}`]: fail(422, 'VALIDATION_ERROR', 'The return was refused', {
          fields: { 'lines.0.quantity': ['Only 30 is still returnable on this line'] },
        }),
      }),
    })

    await openForm(user, fetch)
    await stable(() => screen.getByText('Brass rod 12mm'))
    await user.type(screen.getByRole('textbox', { name: 'Returning' }), '10')
    await user.type(screen.getByRole('textbox', { name: 'Reason' }), 'Damaged in transit')
    await user.click(screen.getByRole('button', { name: 'Raise draft return' }))

    await stable(() => screen.getByText('Only 30 is still returnable on this line'))
    expect(screen.getByRole('dialog')).toBeVisible()
  })

  it('says the receipt cannot be chosen without the right to read receipts', async () => {
    // Searching goods receipts is `grn.view`, a different right from the
    // `purchase_return.create` that opened this dialog. Without it the picker
    // would 403 on every search and look like an empty receipt register.
    const { user, fetch } = renderScreen(<PurchaseReturnsPage />, {
      permissions: [P.PURCHASE_RETURN_VIEW, P.PURCHASE_RETURN_CREATE, P.VENDOR_VIEW],
      routes: {
        [`GET ${RETURNS_PATH}`]: page([purchaseReturn()]),
        'GET /vendors/options': ok([]),
      },
    })

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: /new return/i }))
    const dialog = await stable(() => screen.getByRole('dialog'))
    expect(within(dialog).getByText(/goods receipts are not visible to your account/i)).toBeVisible()
    expect(within(dialog).getByRole('combobox', { name: 'Goods receipt' })).toBeDisabled()
  })

  it('says so when the whole receipt has already gone back', async () => {
    const { user, fetch } = renderScreen(<PurchaseReturnsPage />, {
      permissions: STOREKEEPER,
      routes: formRoutes({
        [`GET /procurement/goods-receipts/${RECEIPT_ID}`]: receiptDetail([fullyReturnedLine()]),
      }),
    })

    await openForm(user, fetch)

    await stable(() => screen.getByText('Nothing on this receipt can be returned'))
    expect(screen.getByRole('button', { name: 'Raise draft return' })).toBeDisabled()
  })
})

describe('the register itself', () => {
  it('says where a return starts when there are none', async () => {
    renderScreen(<PurchaseReturnsPage />, {
      permissions: STOREKEEPER,
      routes: registerRoutes([]),
    })

    await stable(() => screen.getByText('No purchase returns found'))
    expect(screen.getByText(/always starts from a posted goods receipt/i)).toBeVisible()
  })

  it('reports a failed list rather than an empty one', async () => {
    renderScreen(<PurchaseReturnsPage />, {
      permissions: STOREKEEPER,
      routes: registerRoutes([], {
        [`GET ${RETURNS_PATH}`]: fail(500, 'INTERNAL', 'The return register is unavailable'),
      }),
    })

    await stable(() => screen.getByText('Could not load this list'))
    expect(screen.getByText('The return register is unavailable')).toBeVisible()
  })

  it('hides the warehouse filter from an account that cannot read warehouses', async () => {
    renderScreen(<PurchaseReturnsPage />, {
      permissions: [P.PURCHASE_RETURN_VIEW, P.VENDOR_VIEW],
      routes: {
        [`GET ${RETURNS_PATH}`]: page([purchaseReturn()]),
        'GET /vendors/options': ok([]),
      },
    })

    await stable(() => screen.getByRole('button', { name: 'PRET-0001' }))
    // An option list the account cannot populate looks broken rather than
    // unavailable, so the filter is not offered at all.
    expect(screen.queryByRole('combobox', { name: 'Filter by warehouse' })).not.toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Filter by status' })).toBeVisible()
  })
})
