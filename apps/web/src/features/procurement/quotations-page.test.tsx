/**
 * The quotation register and the dialog that decides between offers.
 *
 * Selecting a quotation is the buying decision. It is not a status change with a
 * confirmation on top - it names the vendor this RFQ is ordered against, so it
 * is gated on `quotation.manage` rather than on the `quotation.view` that
 * rendered the table, it demands a recorded reason the API bounds at 3..255
 * characters, and it is shown next to what the choice costs against the other
 * quotes on the board. Every one of those is asserted below.
 *
 * The lifecycle is the other half. `QUOTATION_LIFECYCLE` draws SELECTED and
 * REJECTED edges only from RECEIVED and UNDER_REVIEW, and `update_quotation`
 * refuses a revision only on the winner - a REJECTED or EXPIRED offer is
 * reopened by the revised one - so a chosen offer is offered no command at
 * all rather than a menu that ends in `assert_transition`.
 *
 * And the arithmetic. The premium a buyer reads before committing is derived
 * with decimal.js from the decimal strings the API sent, measured against the
 * cheapest quote that has *not* lapsed - because a saving against an offer the
 * API would refuse to select is a saving nobody can take. On a mixed-currency
 * board the comparison is refused outright rather than approximated.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type FetchRecorder, type RouteTable } from '@/test/harness'

import { QuotationFormDialog } from './quotation-dialogs'
import type { QuotationRow } from './quotation-model'
import QuotationsPage from './quotations-page'

/** Wait for something that arrives after a fetch, then read it fresh. */
async function stable<T extends HTMLElement>(read: () => T): Promise<T> {
  await waitFor(read)
  return read()
}

/**
 * Wait until the screen has stopped fetching.
 *
 * The vendor names come from a reference lookup that settles after the rows do,
 * and the commit that adds them rebuilds the row's DOM: a press that began
 * before that commit finishes on a node no longer in the document, and nothing
 * happens at all. Two polls with no new request means the rows have stopped
 * moving.
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

const QUOTATION_ID = '01a00000-0000-7000-8000-00000000qt01'
const RIVAL_ID = '01a00000-0000-7000-8000-00000000qt02'
const RFQ_ID = '01a00000-0000-7000-8000-0000000rfq01'
const VENDOR_A = '01a00000-0000-7000-8000-00000000ven1'
const VENDOR_B = '01a00000-0000-7000-8000-00000000ven2'
const ITEM_BRASS = '01a00000-0000-7000-8000-0000000item1'
const ITEM_STEEL = '01a00000-0000-7000-8000-0000000item2'
const QUOTATIONS_PATH = '/procurement/quotations'
const COMPARISON_PATH = `/procurement/rfqs/${RFQ_ID}/comparison`

type User = ReturnType<typeof renderScreen>['user']

function quotation(overrides: Partial<QuotationRow> = {}): QuotationRow {
  return {
    id: QUOTATION_ID,
    rfq_id: RFQ_ID,
    vendor_id: VENDOR_A,
    vendor_reference: 'ACME-Q-8891',
    status: 'RECEIVED',
    quotation_date: '2026-09-05',
    valid_until: '2099-12-31',
    currency: 'INR',
    subtotal: '100000.0000',
    tax_amount: '18000.0000',
    discount: '0.0000',
    freight_amount: '0.0000',
    total_amount: '118000.0000',
    lead_time_days: 21,
    payment_terms_days: 30,
    selected_at: null,
    converted_purchase_order_id: null,
    created_at: '2026-09-05T09:00:00Z',
    ...overrides,
  }
}

/**
 * A board on which the offer under test is the dearer of two live quotes.
 *
 * `total_amount` is what the premium is measured from, and both columns are
 * live so the cheaper one is genuinely selectable.
 */
function comparison(overrides: Record<string, unknown> = {}) {
  return ok({
    rfq_id: RFQ_ID,
    rfq_code: 'RFQ-0001',
    rfq_status: 'SENT',
    currency: 'INR',
    mixed_currency: false,
    recommended_vendor_id: VENDOR_B,
    recommendation_reason: 'Boltworks wins 3 of 4 lines',
    vendors: [
      {
        quotation_id: QUOTATION_ID,
        quotation_status: 'RECEIVED',
        vendor_id: VENDOR_A,
        vendor_code: 'V-ACME',
        vendor_name: 'Acme Metals',
        currency: 'INR',
        subtotal: '100000.0000',
        tax_amount: '18000.0000',
        freight_amount: '0.0000',
        total_amount: '118000.0000',
        lead_time_days: 21,
        payment_terms_days: 30,
        valid_until: '2099-12-31',
        is_expired: false,
        quoted_item_count: 4,
        vendor_rating: null,
        rating_sample_size: 0,
      },
      {
        quotation_id: RIVAL_ID,
        quotation_status: 'RECEIVED',
        vendor_id: VENDOR_B,
        vendor_code: 'V-BOLT',
        vendor_name: 'Boltworks',
        currency: 'INR',
        subtotal: '90000.0000',
        tax_amount: '16200.0000',
        freight_amount: '0.0000',
        total_amount: '106200.0000',
        lead_time_days: 28,
        payment_terms_days: 45,
        valid_until: '2099-12-31',
        is_expired: false,
        quoted_item_count: 4,
        vendor_rating: null,
        rating_sample_size: 0,
      },
    ],
    items: [],
    ...overrides,
  })
}

/**
 * The same offer as the detail endpoint returns it.
 *
 * The decision is taken from this dialog rather than from the row's own menu -
 * see the note on `openDetail`.
 */
function quotationDetail(overrides: Record<string, unknown> = {}) {
  return ok({
    ...quotation(overrides as Partial<QuotationRow>),
    terms: null,
    notes: null,
    decision_reason: null,
    selected_by_id: null,
    lines: [
      {
        id: '01a00000-0000-7000-8000-0000000000l1',
        line_number: 1,
        item_id: ITEM_BRASS,
        description: 'Brass rod 12mm',
        quantity: '250.0000',
        unit_price: '400.000000',
        discount_percentage: '0.0000',
        discount: '0.0000',
        tax_percentage: '18.0000',
        tax_amount: '18000.0000',
        line_total: '118000.0000',
        lead_time_days: null,
        uom_id: null,
        notes: null,
      },
    ],
    ...overrides,
  })
}

function registerRoutes(rows: QuotationRow[], extra: RouteTable = {}): RouteTable {
  return {
    [`GET ${QUOTATIONS_PATH}`]: page(rows),
    // The longer path wins the prefix match, so the detail dialog is not
    // served the list envelope by accident.
    [`GET ${QUOTATIONS_PATH}/${QUOTATION_ID}`]: quotationDetail(
      rows[0] ? { status: rows[0].status, valid_until: rows[0].valid_until } : {},
    ),
    'GET /vendors/options': ok([
      { id: VENDOR_A, code: 'V-ACME', name: 'Acme Metals', is_approved: true },
      { id: VENDOR_B, code: 'V-BOLT', name: 'Boltworks', is_approved: true },
    ]),
    ...extra,
  }
}

/** Everything the record-a-quotation dialog reaches for on top of the register. */
function formRoutes(extra: RouteTable = {}): RouteTable {
  return registerRoutes([quotation()], {
    'GET /procurement/rfqs': page([
      {
        id: RFQ_ID,
        code: 'RFQ-0001',
        status: 'SENT',
        rfq_date: '2026-09-01',
        response_due_date: '2026-09-10',
        required_date: '2026-09-30',
        currency: 'INR',
        warehouse_id: '01a00000-0000-7000-8000-00000000wh01',
        requisition_id: null,
        vendor_ids: [VENDOR_A],
        sent_at: '2026-09-02T09:00:00Z',
        closed_at: null,
        created_at: '2026-09-01T09:00:00Z',
      },
    ]),
    'GET /vendors': page([
      { id: VENDOR_A, code: 'V-ACME', name: 'Acme Metals', is_approved: true },
      { id: VENDOR_B, code: 'V-BOLT', name: 'Boltworks', is_approved: true },
    ]),
    'GET /products/items': page([
      { id: ITEM_BRASS, code: 'BRASS-ROD-12', name: 'Brass rod 12mm' },
      { id: ITEM_STEEL, code: 'SS-SHEET-2', name: 'Stainless sheet 2mm' },
    ]),
    ...extra,
  })
}

const READER = [P.QUOTATION_VIEW, P.VENDOR_VIEW]
const BUYER = [...READER, P.QUOTATION_MANAGE, P.RFQ_VIEW, P.ITEM_VIEW]

/**
 * Open one offer and hand back its dialog.
 *
 * The decision is taken from here rather than from the row's overflow menu: the
 * actions cell does not stop the click reaching the row, so pressing the menu
 * opens this dialog instead of the menu (reported separately). The dialog is the
 * route that works, and it is where the footer offers the same three commands
 * behind the same right.
 */
async function openDetail(user: User, fetch: FetchRecorder, reference: string) {
  await settled(fetch)
  await user.click(screen.getByRole('button', { name: reference }))
  return await stable(() => screen.getByRole('dialog'))
}

describe('who may decide', () => {
  it('shows the offers but no commands without quotation.manage', async () => {
    renderScreen(<QuotationsPage />, {
      permissions: READER,
      routes: registerRoutes([quotation()]),
    })

    await stable(() => screen.getByText('ACME-Q-8891'))
    // Selecting, rejecting and revising are all `quotation.manage` on the API -
    // never the `quotation.view` that rendered this table. A reader who is
    // offered the decision is a reader who gets a 403 at the end of it.
    expect(screen.queryByRole('button', { name: /record quotation/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /actions for quotation/i })).not.toBeInTheDocument()
  })

  it('withholds the decision from a reader looking at the offer itself', async () => {
    // The dialog is open to anybody who may read quotations; the buying
    // decision inside it is not. This is the assertion that matters, because
    // `quotation.view` and `quotation.manage` are one letter apart in a gate.
    const { user, fetch } = renderScreen(<QuotationsPage />, {
      permissions: READER,
      routes: registerRoutes([quotation()]),
    })

    const dialog = await openDetail(user, fetch, 'ACME-Q-8891')
    expect(within(dialog).getByText('Quotation ACME-Q-8891')).toBeVisible()
    expect(
      within(dialog).queryByRole('button', { name: 'Select this quotation' }),
    ).not.toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Reject' })).not.toBeInTheDocument()
    expect(
      within(dialog).queryByRole('button', { name: 'Record revised offer' }),
    ).not.toBeInTheDocument()
  })

  it('offers the buying decision once quotation.manage is held', async () => {
    const { user, fetch } = renderScreen(<QuotationsPage />, {
      permissions: BUYER,
      routes: registerRoutes([quotation()]),
    })

    expect(
      await stable(() => screen.getByRole('button', { name: /record quotation/i })),
    ).toBeVisible()
    const dialog = await openDetail(user, fetch, 'ACME-Q-8891')
    expect(within(dialog).getByRole('button', { name: 'Select this quotation' })).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Reject' })).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Record revised offer' })).toBeVisible()
  })
})

describe('the state machine decides which commands exist', () => {
  it('offers the decision on an offer still in contention', async () => {
    const { user, fetch } = renderScreen(<QuotationsPage />, {
      permissions: BUYER,
      routes: registerRoutes([quotation({ status: 'UNDER_REVIEW' })]),
    })

    // RECEIVED and UNDER_REVIEW are the two states the machine draws a SELECTED
    // or REJECTED edge from.
    const dialog = await openDetail(user, fetch, 'ACME-Q-8891')
    expect(within(dialog).getByRole('button', { name: 'Select this quotation' })).toBeVisible()
  })

  it('offers a revision but no decision inside a rejected offer', async () => {
    const { user, fetch } = renderScreen(<QuotationsPage />, {
      permissions: BUYER,
      routes: registerRoutes([quotation({ status: 'REJECTED', valid_until: '2099-12-31' })]),
    })

    // `update_quotation` reopens a REJECTED quotation with the revised offer -
    // the buyer asked the vendor to requote - so the revision stays on offer,
    // while the decision needs an edge the machine only draws from RECEIVED
    // or UNDER_REVIEW.
    const dialog = await openDetail(user, fetch, 'ACME-Q-8891')
    expect(
      within(dialog).queryByRole('button', { name: 'Select this quotation' }),
    ).not.toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Record revised offer' })).toBeVisible()
  })

  it('offers nothing on an offer that has already been chosen', async () => {
    // SELECTED is the one terminal state a revision cannot reopen: a purchase
    // order is raised against it, so `update_quotation` refuses outright.
    renderScreen(<QuotationsPage />, {
      permissions: BUYER,
      routes: registerRoutes([
        quotation({ status: 'SELECTED', selected_at: '2026-09-11T09:00:00Z' }),
      ]),
    })

    await stable(() => screen.getByText('ACME-Q-8891'))
    expect(screen.queryByRole('button', { name: /actions for quotation/i })).not.toBeInTheDocument()
  })

  it('offers a revision on a lapsed offer, and nothing else', async () => {
    const { user, fetch } = renderScreen(<QuotationsPage />, {
      permissions: BUYER,
      routes: registerRoutes([quotation({ status: 'EXPIRED', valid_until: '2020-01-01' })]),
    })

    // A lapsed offer is requoted, not decided: the revision reopens it.
    await stable(() => screen.getByText('ACME-Q-8891'))
    expect(screen.getByRole('button', { name: /actions for quotation/i })).toBeVisible()
    const dialog = await openDetail(user, fetch, 'ACME-Q-8891')
    expect(within(dialog).getByRole('button', { name: 'Record revised offer' })).toBeVisible()
    expect(
      within(dialog).queryByRole('button', { name: 'Select this quotation' }),
    ).not.toBeInTheDocument()
  })

  it('warns that an offer looks lapsed without hiding the decision', async () => {
    // The API compares `valid_until` against the *server's* date and refuses a
    // lapsed selection itself. Hiding the action on the browser's reading would
    // disagree with it across a date boundary in either direction.
    renderScreen(<QuotationsPage />, {
      permissions: BUYER,
      routes: registerRoutes([quotation({ valid_until: '2020-01-01' })]),
    })

    await stable(() => screen.getByText('Lapsed'))
    expect(screen.getByRole('button', { name: 'Actions for quotation ACME-Q-8891' })).toBeVisible()
  })

  it('says open-ended rather than showing a lapsed badge with no date', async () => {
    renderScreen(<QuotationsPage />, {
      permissions: BUYER,
      routes: registerRoutes([quotation({ valid_until: null })]),
    })

    // Nullable, and a different claim from a date in the past.
    await stable(() => screen.getByText('Open-ended'))
    expect(screen.queryByText('Lapsed')).not.toBeInTheDocument()
  })
})

describe('the buying decision', () => {
  it('will not select without the reason the API records', async () => {
    const { user, fetch } = renderScreen(<QuotationsPage />, {
      permissions: BUYER,
      routes: registerRoutes([quotation()], { [`GET ${COMPARISON_PATH}`]: comparison() }),
    })

    const dialog = await openDetail(user, fetch, 'ACME-Q-8891')
    await user.click(within(dialog).getByRole('button', { name: 'Select this quotation' }))
    await user.click(
      await stable(() => screen.getByRole('button', { name: 'Select this quotation' })),
    )

    // `QuotationDecisionRequest` makes the reason mandatory on both decisions:
    // the buyer's judgement is the record of why this vendor was chosen.
    await stable(() => screen.getByText(/record at least 3 characters of reason/i))
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('posts the selection to the vendor’s own quotation with the reason trimmed', async () => {
    const selectPath = `${QUOTATIONS_PATH}/${QUOTATION_ID}/select`
    const { user, fetch } = renderScreen(<QuotationsPage />, {
      permissions: BUYER,
      routes: registerRoutes([quotation()], {
        [`GET ${COMPARISON_PATH}`]: comparison(),
        [`POST ${selectPath}`]: ok(quotation({ status: 'SELECTED' })),
      }),
    })

    const dialog = await openDetail(user, fetch, 'ACME-Q-8891')
    await user.click(within(dialog).getByRole('button', { name: 'Select this quotation' }))
    await user.type(
      await stable(() => screen.getByRole('textbox', { name: 'Reason' })),
      '  Delivers three weeks earlier  ',
    )
    await user.click(screen.getByRole('button', { name: 'Select this quotation' }))

    await waitFor(() => fetch.requestFor('POST', selectPath))
    expect(fetch.requestFor('POST', selectPath)).toEqual({
      reason: 'Delivers three weeks earlier',
    })
  })

  it('posts a rejection to the reject endpoint, not the select one', async () => {
    const rejectPath = `${QUOTATIONS_PATH}/${QUOTATION_ID}/reject`
    const { user, fetch } = renderScreen(<QuotationsPage />, {
      permissions: BUYER,
      routes: registerRoutes([quotation()], {
        [`GET ${COMPARISON_PATH}`]: comparison(),
        [`POST ${rejectPath}`]: ok(quotation({ status: 'REJECTED' })),
      }),
    })

    const dialog = await openDetail(user, fetch, 'ACME-Q-8891')
    await user.click(within(dialog).getByRole('button', { name: 'Reject' }))
    await user.type(
      await stable(() => screen.getByRole('textbox', { name: 'Reason' })),
      'Lead time will not meet the build slot',
    )
    await user.click(screen.getByRole('button', { name: 'Reject quotation' }))

    await waitFor(() => fetch.requestFor('POST', rejectPath))
    // The two decisions are separate endpoints and the wrong one closes out the
    // offer the buyer meant to accept.
    expect(fetch.calls.some((call) => call.path.endsWith('/select'))).toBe(false)
  })

  it('shows what the choice costs against the cheapest live quote', async () => {
    const { user, fetch } = renderScreen(<QuotationsPage />, {
      permissions: BUYER,
      routes: registerRoutes([quotation()], { [`GET ${COMPARISON_PATH}`]: comparison() }),
    })

    const dialog = await openDetail(user, fetch, 'ACME-Q-8891')
    await user.click(within(dialog).getByRole('button', { name: 'Select this quotation' }))

    // 118,000 against 106,200 is 11,800 more - derived with decimal.js from the
    // decimal strings the API sent, which is the one number on this screen that
    // must not pass through a float.
    await stable(() => screen.getByText(/^₹\s*11,800\.00$/))
    expect(screen.getByText(/the cheapest offer still valid/i)).toBeVisible()
    // And the same gap again on the rival row, signed the other way round.
    expect(screen.getByText('+11,800.00')).toBeVisible()
    // And the API named a different winner, which the buyer is told before they
    // commit rather than after.
    expect(screen.getByText('Not the recommended vendor')).toBeVisible()
    expect(screen.getByText('Boltworks wins 3 of 4 lines')).toBeVisible()
  })

  it('refuses to compare magnitudes across currencies', async () => {
    // 100 EUR against 9,000 INR would recommend the wrong vendor with total
    // confidence. The API declines to recommend at all and says why; that
    // sentence is shown instead of arithmetic this screen cannot do.
    const { user, fetch } = renderScreen(<QuotationsPage />, {
      permissions: BUYER,
      routes: registerRoutes([quotation()], {
        [`GET ${COMPARISON_PATH}`]: comparison({
          mixed_currency: true,
          recommended_vendor_id: null,
          recommendation_reason: 'Vendors quoted in different currencies, so no ranking is offered',
        }),
      }),
    })

    const dialog = await openDetail(user, fetch, 'ACME-Q-8891')
    await user.click(within(dialog).getByRole('button', { name: 'Select this quotation' }))

    await stable(() => screen.getByText('Prices are not comparable'))
    expect(screen.queryByText(/the cheapest offer still valid/i)).not.toBeInTheDocument()
  })

  it('lets the decision be taken when the board itself will not load', async () => {
    // The comparison is context, not a precondition: a buyer who decided on
    // paper must not be blocked by a panel that failed to load.
    const selectPath = `${QUOTATIONS_PATH}/${QUOTATION_ID}/select`
    const { user, fetch } = renderScreen(<QuotationsPage />, {
      permissions: BUYER,
      routes: registerRoutes([quotation()], {
        [`GET ${COMPARISON_PATH}`]: fail(500, 'INTERNAL', 'The comparison could not be built'),
        [`POST ${selectPath}`]: ok(quotation({ status: 'SELECTED' })),
      }),
    })

    const dialog = await openDetail(user, fetch, 'ACME-Q-8891')
    await user.click(within(dialog).getByRole('button', { name: 'Select this quotation' }))
    await stable(() => screen.getByText('The other quotations could not be loaded'))

    await user.type(
      screen.getByRole('textbox', { name: 'Reason' }),
      'Agreed with production this morning',
    )
    await user.click(screen.getByRole('button', { name: 'Select this quotation' }))

    await waitFor(() => fetch.requestFor('POST', selectPath))
    expect(fetch.requestFor('POST', selectPath)).toEqual({
      reason: 'Agreed with production this morning',
    })
  })
})

describe('what the quotation form sends', () => {
  async function openForm(user: User, fetch: FetchRecorder) {
    await settled(fetch)
    await user.click(screen.getByRole('button', { name: /record quotation/i }))
    await stable(() => screen.getByRole('dialog'))
    await user.click(screen.getByRole('combobox', { name: 'RFQ' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /RFQ-0001/ })))
    await user.click(screen.getByRole('combobox', { name: 'Vendor' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /V-ACME/ })))
    await user.type(screen.getByRole('textbox', { name: /vendor quotation number/i }), 'ACME-Q-9')
  }

  async function fillLine(
    user: User,
    index: number,
    item: RegExp,
    quantity: string,
    price: string,
  ) {
    await user.click(screen.getByRole('combobox', { name: `Line ${index + 1} item` }))
    await user.click(await stable(() => screen.getByRole('option', { name: item })))
    await user.type(screen.getAllByRole('textbox', { name: 'Quantity' })[index]!, quantity)
    await user.type(screen.getAllByRole('textbox', { name: 'Unit price' })[index]!, price)
  }

  it('sends two lines, with the money still strings and the blanks as null', async () => {
    const { user, fetch } = renderScreen(<QuotationsPage />, {
      permissions: BUYER,
      routes: formRoutes({
        [`POST ${QUOTATIONS_PATH}`]: ok({
          vendor_reference: 'ACME-Q-9',
          total_amount: '1000.0000',
          currency: 'INR',
        }),
      }),
    })

    await openForm(user, fetch)
    await fillLine(user, 0, /BRASS-ROD-12/, '250', '340.250000')
    await user.click(screen.getByRole('button', { name: /add line/i }))
    await fillLine(user, 1, /SS-SHEET-2/, '18.75', '1200')
    await user.type(screen.getAllByRole('textbox', { name: 'Discount %' })[1]!, '2.5')

    await user.click(screen.getByRole('button', { name: 'Record quotation' }))

    await waitFor(() => fetch.requestFor('POST', QUOTATIONS_PATH))
    const body = fetch.requestFor('POST', QUOTATIONS_PATH) as Record<string, unknown> & {
      lines: Record<string, unknown>[]
    }

    expect(body.rfq_id).toBe(RFQ_ID)
    expect(body.vendor_id).toBe(VENDOR_A)
    expect(body.currency).toBe('INR')
    // Blank terms mean "whatever the vendor master says": the keys are omitted
    // so `create_quotation` falls back to the vendor's own terms, where a zero
    // would have reached it as a literal zero days.
    expect(body).not.toHaveProperty('payment_terms_days')
    expect(body).not.toHaveProperty('lead_time_days')
    // Freight is a decimal string on the wire even when it is nothing.
    expect(body.freight_amount).toBe('0')
    expect(body.valid_until).toBeNull()

    expect(body.lines[0]).toEqual({
      line_number: 1,
      item_id: ITEM_BRASS,
      quantity: '250',
      unit_price: '340.250000',
      discount_percentage: null,
      // Null means "the header's lead time applies", which is a different
      // statement from promising zero days.
      lead_time_days: null,
    })
    expect(body.lines[1]).toMatchObject({
      line_number: 2,
      item_id: ITEM_STEEL,
      quantity: '18.75',
      unit_price: '1200',
      discount_percentage: '2.5',
    })
    // `uom_id` is deliberately absent so the API falls back to the item's own
    // order unit, and there is no tax field at all - the item master owns it.
    expect(body.lines[0]).not.toHaveProperty('uom_id')
    expect(body.lines[0]).not.toHaveProperty('tax_percentage')
    expect(typeof body.lines[0]!.unit_price).toBe('string')
  })

  it('renumbers the line that survives a removal', async () => {
    const { user, fetch } = renderScreen(<QuotationsPage />, {
      permissions: BUYER,
      routes: formRoutes({
        [`POST ${QUOTATIONS_PATH}`]: ok({
          vendor_reference: 'ACME-Q-9',
          total_amount: '1000.0000',
          currency: 'INR',
        }),
      }),
    })

    await openForm(user, fetch)
    await fillLine(user, 0, /BRASS-ROD-12/, '1', '10')
    await user.click(screen.getByRole('button', { name: /add line/i }))
    await fillLine(user, 1, /SS-SHEET-2/, '2', '20')
    await user.click(screen.getByRole('button', { name: 'Remove line 1' }))

    await user.click(screen.getByRole('button', { name: 'Record quotation' }))

    await waitFor(() => fetch.requestFor('POST', QUOTATIONS_PATH))
    const body = fetch.requestFor('POST', QUOTATIONS_PATH) as { lines: Record<string, unknown>[] }
    expect(body.lines).toEqual([
      expect.objectContaining({ line_number: 1, item_id: ITEM_STEEL, unit_price: '20' }),
    ])
  })

  it('refuses an offer that expired before it was made', async () => {
    const { user, fetch } = renderScreen(<QuotationsPage />, {
      permissions: BUYER,
      routes: formRoutes(),
    })

    await openForm(user, fetch)
    await fillLine(user, 0, /BRASS-ROD-12/, '1', '10')
    await user.type(screen.getByLabelText(/valid until/i), '2020-01-01')
    await user.click(screen.getByRole('button', { name: 'Record quotation' }))

    // A keying slip, and it would be refused for selection the moment anybody
    // tried to act on it.
    await stable(() => screen.getByText(/an offer cannot expire before it was quoted/i))
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('lands a server field error on the line that caused it', async () => {
    const { user, fetch } = renderScreen(<QuotationsPage />, {
      permissions: BUYER,
      routes: formRoutes({
        [`POST ${QUOTATIONS_PATH}`]: fail(422, 'VALIDATION_ERROR', 'Could not record the offer', {
          fields: { 'lines.0.unit_price': ['A price is required on every quoted line'] },
        }),
      }),
    })

    await openForm(user, fetch)
    await fillLine(user, 0, /BRASS-ROD-12/, '1', '10')
    await user.click(screen.getByRole('button', { name: 'Record quotation' }))

    await stable(() => screen.getByText('A price is required on every quoted line'))
    expect(screen.getByRole('dialog')).toBeVisible()
  })

  it('says the lines cannot be keyed when the item master is unreadable', async () => {
    // The line editor searches `/products/items`, which enforces `item.view` -
    // not the `quotation.manage` that lets this form post.
    const { user, fetch } = renderScreen(<QuotationsPage />, {
      permissions: [P.QUOTATION_VIEW, P.QUOTATION_MANAGE, P.VENDOR_VIEW, P.RFQ_VIEW],
      routes: registerRoutes([quotation()], {
        'GET /procurement/rfqs': page([]),
        'GET /vendors': page([]),
      }),
    })

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: /record quotation/i }))
    const dialog = await stable(() => screen.getByRole('dialog'))
    expect(within(dialog).getByText(/cannot key the lines from here/i)).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Record quotation' })).toBeDisabled()
  })
})

describe('revising an offer that will not load', () => {
  it('offers nothing to save until the quotation has actually loaded', async () => {
    // The dialog stays mounted between opens, so an unguarded form would hold
    // whatever the last quotation left in it - and `update_quotation` replaces
    // the whole line set with whatever is shown. Nothing editable, and no
    // Save, until the revision has loaded.
    let attempts = 0
    const { user } = renderScreen(
      <QuotationFormDialog open onOpenChange={() => undefined} quotationId={QUOTATION_ID} />,
      {
        permissions: BUYER,
        routes: {
          [`GET ${QUOTATIONS_PATH}/${QUOTATION_ID}`]: () => {
            attempts += 1
            return attempts === 1
              ? fail(500, 'INTERNAL', 'The quotation could not be read')
              : quotationDetail()
          },
          // The line's item is resolved by id so the picker reads as a code.
          [`GET /products/items/${ITEM_BRASS}`]: ok({
            id: ITEM_BRASS,
            code: 'BRASS-ROD-12',
            name: 'Brass rod 12mm',
          }),
        },
      },
    )

    const dialog = await stable(() => screen.getByRole('dialog'))
    await stable(() => within(dialog).getByText('Could not load this quotation'))
    expect(within(dialog).getByText('The quotation could not be read')).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Save revised offer' })).toBeDisabled()
    expect(
      within(dialog).queryByRole('textbox', { name: /vendor quotation number/i }),
    ).not.toBeInTheDocument()
    expect(within(dialog).queryByRole('textbox', { name: 'Unit price' })).not.toBeInTheDocument()

    // Try again brings the offer, and only then the fields - filled from it.
    await user.click(within(dialog).getByRole('button', { name: /try again/i }))
    await stable(() => screen.getByRole('textbox', { name: /vendor quotation number/i }))
    expect(screen.getByRole('textbox', { name: /vendor quotation number/i })).toHaveValue(
      'ACME-Q-8891',
    )
    expect(screen.getByRole('button', { name: 'Save revised offer' })).toBeEnabled()
  })
})

describe('the register itself', () => {
  it('explains where quotations come from when there are none', async () => {
    renderScreen(<QuotationsPage />, { permissions: BUYER, routes: registerRoutes([]) })

    await stable(() => screen.getByText('No quotations found'))
    expect(screen.getByText(/record what each one quoted/i)).toBeVisible()
  })

  it('reports a failed list rather than an empty one', async () => {
    renderScreen(<QuotationsPage />, {
      permissions: BUYER,
      routes: registerRoutes([], {
        [`GET ${QUOTATIONS_PATH}`]: fail(500, 'INTERNAL', 'The quotation service is unavailable'),
      }),
    })

    await stable(() => screen.getByText('Could not load this list'))
    expect(screen.getByText('The quotation service is unavailable')).toBeVisible()
  })

  it('marks an offer that has already been turned into an order', async () => {
    renderScreen(<QuotationsPage />, {
      permissions: BUYER,
      routes: registerRoutes([
        quotation({
          status: 'SELECTED',
          converted_purchase_order_id: '01a00000-0000-7000-8000-000000000po1',
        }),
      ]),
    })

    // Selected and ordered are two different facts, and a buyer chasing an
    // order needs to see which one this offer has reached.
    await stable(() => screen.getByText('Ordered'))
  })
})
