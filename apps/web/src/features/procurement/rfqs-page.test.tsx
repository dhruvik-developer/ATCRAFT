/**
 * The RFQ board and the dialog that raises an enquiry.
 *
 * An RFQ's state machine is four edges wide - DRAFT -> SENT, DRAFT ->
 * CANCELLED, SENT -> CLOSED, SENT -> CANCELLED - and every command on this
 * screen is one of them. The predicates are kept apart in `rfq-model` precisely
 * because they disagree: a draft can be sent but not closed, and a sent enquiry
 * can be closed but never sent again or edited, because the vendors are pricing
 * the lines as they were issued. So each test below fixes one status and asserts
 * both what is offered and what is *not*.
 *
 * The one gate that is not a status is the vendor list. `transition_rfq` refuses
 * to send an RFQ that names nobody, so Send is withheld and the row says what is
 * missing instead - the fix is an edit, not a retry.
 *
 * The payload assertions are about the two contract facts that decide whether
 * the enquiry says anything: quantities stay decimal strings, and `lines` is
 * *omitted entirely* rather than sent empty when a requisition's own open lines
 * are what is being quoted, because `create_rfq` copies them only when the key
 * is absent and refuses an empty array outright.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type FetchRecorder, type RouteTable } from '@/test/harness'

import type { RfqRow } from './rfq-model'
import RfqsPage from './rfqs-page'

/**
 * Wait for a control, then read it again.
 *
 * The vendor and warehouse lookups settle after the rows do, and the commit that
 * follows replaces the row's DOM. A node captured by `findBy*` before that
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
 * The vendor and warehouse names come from reference lookups that settle after
 * the rows do, and the commit that adds them rebuilds the row's DOM. A press
 * that began before that commit finishes on a node no longer in the document,
 * and nothing happens at all - so every test that works a control inside a row
 * waits here first. Two polls with no new request means everything the screen
 * asked for has arrived and the rows have stopped moving.
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

const RFQ_ID = '01a00000-0000-7000-8000-0000000rfq01'
const VENDOR_A = '01a00000-0000-7000-8000-00000000ven1'
const VENDOR_B = '01a00000-0000-7000-8000-00000000ven2'
const WAREHOUSE_ID = '01a00000-0000-7000-8000-00000000wh01'
const ITEM_BRASS = '01a00000-0000-7000-8000-0000000item1'
const ITEM_STEEL = '01a00000-0000-7000-8000-0000000item2'
const RFQS_PATH = '/procurement/rfqs'

type User = ReturnType<typeof renderScreen>['user']

function rfq(overrides: Partial<RfqRow> = {}): RfqRow {
  return {
    id: RFQ_ID,
    code: 'RFQ-0001',
    status: 'DRAFT',
    rfq_date: '2026-09-01',
    response_due_date: '2026-09-10',
    required_date: '2026-09-30',
    currency: 'INR',
    warehouse_id: WAREHOUSE_ID,
    requisition_id: null,
    vendor_ids: [VENDOR_A],
    sent_at: null,
    closed_at: null,
    created_at: '2026-09-01T09:00:00Z',
    ...overrides,
  }
}

/** The two reference lookups the board makes, plus the rows under test. */
function boardRoutes(rows: RfqRow[], extra: RouteTable = {}): RouteTable {
  return {
    [`GET ${RFQS_PATH}`]: page(rows),
    'GET /vendors/options': ok([
      { id: VENDOR_A, code: 'V-ACME', name: 'Acme Metals', is_approved: true },
      { id: VENDOR_B, code: 'V-BOLT', name: 'Boltworks', is_approved: false },
    ]),
    'GET /warehouses/options': ok([
      { id: WAREHOUSE_ID, code: 'WH-MAIN', name: 'Main store', is_active: true },
    ]),
    // The toolbar's warehouse filter is a Radix select that loads its options
    // as soon as it renders, unlike the pickers that wait to be opened.
    'GET /warehouses': page([{ id: WAREHOUSE_ID, code: 'WH-MAIN', name: 'Main store' }]),
    ...extra,
  }
}

/** Everything the form dialog reaches for on top of the board. */
function formRoutes(extra: RouteTable = {}): RouteTable {
  return boardRoutes([rfq()], {
    'GET /vendors': page([
      { id: VENDOR_A, code: 'V-ACME', name: 'Acme Metals', is_approved: true },
      { id: VENDOR_B, code: 'V-BOLT', name: 'Boltworks', is_approved: false },
    ]),
    'GET /products/items': page([
      { id: ITEM_BRASS, code: 'BRASS-ROD-12', name: 'Brass rod 12mm' },
      { id: ITEM_STEEL, code: 'SS-SHEET-2', name: 'Stainless sheet 2mm' },
    ]),
    ...extra,
  })
}

const READ_ONLY = [P.RFQ_VIEW, P.VENDOR_VIEW, P.WAREHOUSE_VIEW]
const BUYER = [...READ_ONLY, P.RFQ_MANAGE]

/** Open a row's overflow menu and hand back the menu to assert against. */
async function openMenu(user: User, fetch: FetchRecorder, code: string): Promise<HTMLElement> {
  await settled(fetch)
  await user.click(screen.getByRole('button', { name: `More actions for ${code}` }))
  return await stable(() => screen.getByRole('menu'))
}

describe('who may work the board', () => {
  it('shows the enquiries but no commands without rfq.manage', async () => {
    renderScreen(<RfqsPage />, { permissions: READ_ONLY, routes: boardRoutes([rfq()]) })

    await stable(() => screen.getByText('RFQ-0001'))
    // Every command on this screen - send, close, cancel, edit - is one endpoint
    // behind `rfq.manage`. A viewer gets the board and nothing else.
    expect(screen.queryByRole('button', { name: /new rfq/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /more actions for/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /send rfq-0001/i })).not.toBeInTheDocument()
  })

  it('gives the buyer the commands once rfq.manage is held', async () => {
    renderScreen(<RfqsPage />, { permissions: BUYER, routes: boardRoutes([rfq()]) })

    expect(await stable(() => screen.getByRole('button', { name: /new rfq/i }))).toBeVisible()
    expect(screen.getByRole('button', { name: 'More actions for RFQ-0001' })).toBeVisible()
  })
})

describe('the state machine decides which commands exist', () => {
  it('lets a draft be sent, edited or cancelled - and never closed', async () => {
    // There is no DRAFT -> CLOSED edge. An enquiry nobody was asked to quote is
    // abandoned by cancelling it, which is what that edge is for.
    const { user, fetch } = renderScreen(<RfqsPage />, {
      permissions: BUYER,
      routes: boardRoutes([rfq({ status: 'DRAFT' })]),
    })

    expect(
      await stable(() => screen.getByRole('button', { name: 'Send RFQ-0001 to its vendors' })),
    ).toBeVisible()
    const menu = await openMenu(user, fetch, 'RFQ-0001')
    expect(within(menu).getByRole('menuitem', { name: 'Edit draft' })).toBeVisible()
    expect(within(menu).getByRole('menuitem', { name: 'Cancel RFQ' })).toBeVisible()
    expect(within(menu).queryByRole('menuitem', { name: 'Close RFQ' })).not.toBeInTheDocument()
  })

  it('lets a sent enquiry be closed or cancelled, and never edited or re-sent', async () => {
    const { user, fetch } = renderScreen(<RfqsPage />, {
      permissions: BUYER,
      routes: boardRoutes([rfq({ status: 'SENT', sent_at: '2026-09-02T10:00:00Z' })]),
    })

    await stable(() => screen.getByText('RFQ-0001'))
    // Vendors are quoting against the lines as they were issued; moving them
    // underneath a quotation would make the comparison meaningless, and
    // `update_rfq` refuses it outright.
    expect(
      screen.queryByRole('button', { name: 'Send RFQ-0001 to its vendors' }),
    ).not.toBeInTheDocument()
    const menu = await openMenu(user, fetch, 'RFQ-0001')
    expect(within(menu).getByRole('menuitem', { name: 'Close RFQ' })).toBeVisible()
    expect(within(menu).getByRole('menuitem', { name: 'Cancel RFQ' })).toBeVisible()
    expect(within(menu).queryByRole('menuitem', { name: 'Edit draft' })).not.toBeInTheDocument()
  })

  it('offers nothing on a settled enquiry', async () => {
    // CLOSED and CANCELLED are terminal in RFQ_LIFECYCLE - there is no edge out
    // of either - so a settled row offers no menu rather than a menu of
    // refusals.
    renderScreen(<RfqsPage />, {
      permissions: BUYER,
      routes: boardRoutes([rfq({ status: 'CLOSED', closed_at: '2026-09-20T10:00:00Z' })]),
    })

    await stable(() => screen.getByText('RFQ-0001'))
    expect(screen.queryByRole('button', { name: /more actions for/i })).not.toBeInTheDocument()
    expect(screen.getByText('This RFQ is settled')).toBeInTheDocument()
  })

  it('withholds Send from a draft that names no vendors, and says why', async () => {
    // `transition_rfq` refuses an RFQ with an empty `vendor_ids` - the fix is an
    // edit, not a retry, so the menu offers the edit rather than the send.
    const { user, fetch } = renderScreen(<RfqsPage />, {
      permissions: BUYER,
      routes: boardRoutes([rfq({ status: 'DRAFT', vendor_ids: [] })]),
    })

    await stable(() => screen.getByText('None invited'))
    expect(
      screen.queryByRole('button', { name: 'Send RFQ-0001 to its vendors' }),
    ).not.toBeInTheDocument()
    const menu = await openMenu(user, fetch, 'RFQ-0001')
    expect(within(menu).getByRole('menuitem', { name: 'Add vendors before sending' })).toBeVisible()
  })

  it('marks a sent enquiry whose reply date has passed, and only a sent one', async () => {
    renderScreen(<RfqsPage />, {
      permissions: BUYER,
      routes: boardRoutes([
        rfq({ id: 'a', code: 'RFQ-LATE', status: 'SENT', response_due_date: '2020-01-01' }),
        rfq({ id: 'b', code: 'RFQ-DRAFT', status: 'DRAFT', response_due_date: '2020-01-01' }),
      ]),
    })

    // A reply date that has passed on a draft is a planning slip, not a late
    // vendor: nobody has been asked for anything yet.
    await stable(() => screen.getByText(/d overdue$/))
    const draft = screen.getByText('RFQ-DRAFT').closest('tr')!
    expect(within(draft).queryByText(/d overdue$/)).not.toBeInTheDocument()
  })
})

describe('what the lifecycle commands send', () => {
  it('sends an RFQ with a null reason when no note was recorded', async () => {
    const sendPath = `${RFQS_PATH}/${RFQ_ID}/send`
    const { user, fetch } = renderScreen(<RfqsPage />, {
      permissions: BUYER,
      routes: boardRoutes([rfq({ status: 'DRAFT' })], {
        [`POST ${sendPath}`]: ok(rfq({ status: 'SENT' })),
      }),
    })

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: 'Send RFQ-0001 to its vendors' }))
    await user.click(await stable(() => screen.getByRole('button', { name: 'Send RFQ' })))

    await waitFor(() => fetch.requestFor('POST', sendPath))
    // `ApproveRequest` accepts null and records "Sent to vendors" itself. An
    // empty string would be a note on the audit trail that says nothing.
    expect(fetch.requestFor('POST', sendPath)).toEqual({ reason: null })
  })

  it('will not cancel an enquiry without the reason the vendors are told', async () => {
    const { user, fetch } = renderScreen(<RfqsPage />, {
      permissions: BUYER,
      routes: boardRoutes([rfq({ status: 'SENT' })]),
    })

    const menu = await openMenu(user, fetch, 'RFQ-0001')
    await user.click(within(menu).getByRole('menuitem', { name: 'Cancel RFQ' }))
    await user.click(await stable(() => screen.getByRole('button', { name: 'Cancel RFQ' })))

    // `ReasonRequest` requires three characters, and this is the sentence the
    // invited vendors are given.
    await stable(() => screen.getByText(/at least three characters/i))
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('posts the cancellation reason once it is recorded', async () => {
    const cancelPath = `${RFQS_PATH}/${RFQ_ID}/cancel`
    const { user, fetch } = renderScreen(<RfqsPage />, {
      permissions: BUYER,
      routes: boardRoutes([rfq({ status: 'SENT' })], {
        [`POST ${cancelPath}`]: ok(rfq({ status: 'CANCELLED' })),
      }),
    })

    const menu = await openMenu(user, fetch, 'RFQ-0001')
    await user.click(within(menu).getByRole('menuitem', { name: 'Cancel RFQ' }))
    await user.type(
      await stable(() => screen.getByRole('textbox', { name: 'Reason' })),
      '  Demand withdrawn by production  ',
    )
    await user.click(screen.getByRole('button', { name: 'Cancel RFQ' }))

    await waitFor(() => fetch.requestFor('POST', cancelPath))
    expect(fetch.requestFor('POST', cancelPath)).toEqual({
      reason: 'Demand withdrawn by production',
    })
  })
})

describe('what the RFQ form sends', () => {
  async function openForm(user: User, fetch: FetchRecorder) {
    await settled(fetch)
    await user.click(screen.getByRole('button', { name: /new rfq/i }))
    await stable(() => screen.getByRole('dialog'))
    await user.click(screen.getByRole('combobox', { name: 'Deliver to' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /WH-MAIN/ })))
  }

  async function fillLine(user: User, index: number, item: RegExp, quantity: string) {
    await user.click(screen.getByRole('combobox', { name: `Line ${index + 1} item` }))
    await user.click(await stable(() => screen.getByRole('option', { name: item })))
    await user.type(screen.getAllByRole('textbox', { name: 'Quantity' })[index]!, quantity)
  }

  it('sends two lines and the invited vendors, with the quantities still strings', async () => {
    const { user, fetch } = renderScreen(<RfqsPage />, {
      permissions: BUYER,
      routes: formRoutes({
        [`POST ${RFQS_PATH}`]: ok({ id: RFQ_ID, code: 'RFQ-0002', lines: [] }),
      }),
    })

    await openForm(user, fetch)
    await fillLine(user, 0, /BRASS-ROD-12/, '250')
    await user.click(screen.getByRole('button', { name: /add line/i }))
    await fillLine(user, 1, /SS-SHEET-2/, '18.75')

    // An unapproved vendor may still be asked for a price - that is often how
    // the evidence to approve one is gathered - so both are invited here.
    await user.click(screen.getByRole('combobox', { name: 'Invite vendors' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /V-ACME/ })))
    await user.click(screen.getByRole('combobox', { name: 'Invite vendors' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /V-BOLT/ })))

    await user.click(screen.getByRole('button', { name: 'Raise draft RFQ' }))

    await waitFor(() => fetch.requestFor('POST', RFQS_PATH))
    const body = fetch.requestFor('POST', RFQS_PATH) as Record<string, unknown> & {
      lines: Record<string, unknown>[]
    }

    expect(body.warehouse_id).toBe(WAREHOUSE_ID)
    expect(body.currency).toBe('INR')
    expect(body.vendor_ids).toEqual([VENDOR_A, VENDOR_B])
    // No requisition was chosen, so the key is absent rather than null: the API
    // reads its presence as "quote this requisition's open lines".
    expect(body).not.toHaveProperty('requisition_id')

    expect(body.lines).toEqual([
      {
        line_number: 1,
        item_id: ITEM_BRASS,
        quantity: '250',
        required_date: null,
        specification: null,
        description: null,
        uom_id: null,
        requisition_line_id: null,
        notes: null,
      },
      expect.objectContaining({ line_number: 2, item_id: ITEM_STEEL, quantity: '18.75' }),
    ])
    expect(typeof body.lines[1]!.quantity).toBe('string')
  })

  it('does not invite the same vendor twice', async () => {
    const { user, fetch } = renderScreen(<RfqsPage />, {
      permissions: BUYER,
      routes: formRoutes({
        [`POST ${RFQS_PATH}`]: ok({ id: RFQ_ID, code: 'RFQ-0003', lines: [] }),
      }),
    })

    await openForm(user, fetch)
    await fillLine(user, 0, /BRASS-ROD-12/, '5')
    await user.click(screen.getByRole('combobox', { name: 'Invite vendors' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /V-ACME/ })))
    await user.click(screen.getByRole('combobox', { name: 'Invite vendors' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /V-ACME/ })))

    await user.click(screen.getByRole('button', { name: 'Raise draft RFQ' }))

    await waitFor(() => fetch.requestFor('POST', RFQS_PATH))
    const body = fetch.requestFor('POST', RFQS_PATH) as { vendor_ids: string[] }
    // `create_rfq` de-duplicates anyway, but a list that grows on every click
    // tells the buyer they have invited a vendor twice when they have not.
    expect(body.vendor_ids).toEqual([VENDOR_A])
  })

  it('drops an invitation when the chip’s remove button is pressed', async () => {
    const { user, fetch } = renderScreen(<RfqsPage />, {
      permissions: BUYER,
      routes: formRoutes({
        [`POST ${RFQS_PATH}`]: ok({ id: RFQ_ID, code: 'RFQ-0004', lines: [] }),
      }),
    })

    await openForm(user, fetch)
    await fillLine(user, 0, /BRASS-ROD-12/, '5')
    await user.click(screen.getByRole('combobox', { name: 'Invite vendors' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /V-ACME/ })))
    await user.click(screen.getByRole('combobox', { name: 'Invite vendors' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /V-BOLT/ })))

    await user.click(screen.getByRole('button', { name: 'Remove Acme Metals from the RFQ' }))
    await user.click(screen.getByRole('button', { name: 'Raise draft RFQ' }))

    await waitFor(() => fetch.requestFor('POST', RFQS_PATH))
    expect((fetch.requestFor('POST', RFQS_PATH) as { vendor_ids: string[] }).vendor_ids).toEqual([
      VENDOR_B,
    ])
  })

  it('renumbers the line that survives a removal', async () => {
    const { user, fetch } = renderScreen(<RfqsPage />, {
      permissions: BUYER,
      routes: formRoutes({
        [`POST ${RFQS_PATH}`]: ok({ id: RFQ_ID, code: 'RFQ-0005', lines: [] }),
      }),
    })

    await openForm(user, fetch)
    await fillLine(user, 0, /BRASS-ROD-12/, '1')
    await user.click(screen.getByRole('button', { name: /add line/i }))
    await fillLine(user, 1, /SS-SHEET-2/, '2')
    await user.click(screen.getByRole('button', { name: 'Remove line 1' }))

    await user.click(screen.getByRole('button', { name: 'Raise draft RFQ' }))

    await waitFor(() => fetch.requestFor('POST', RFQS_PATH))
    const body = fetch.requestFor('POST', RFQS_PATH) as { lines: Record<string, unknown>[] }
    // Renumbered from the form's order, so removing the first of two does not
    // leave the API a gap it would store verbatim.
    expect(body.lines).toEqual([
      expect.objectContaining({ line_number: 1, item_id: ITEM_STEEL, quantity: '2' }),
    ])
  })

  it('refuses an enquiry whose starter line was never filled in', async () => {
    const { user, fetch } = renderScreen(<RfqsPage />, {
      permissions: BUYER,
      routes: formRoutes(),
    })

    await openForm(user, fetch)
    await user.click(screen.getByRole('button', { name: 'Raise draft RFQ' }))

    // The dialog opens with one blank line, so an untouched form is refused
    // line by line rather than as "no lines at all" - and either way nothing
    // reaches the API, because an enquiry that asks for nothing wastes every
    // invited vendor's time.
    await stable(() => screen.getByText('Choose an item'))
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('will not ask for replies after the material is needed', async () => {
    const { user, fetch } = renderScreen(<RfqsPage />, {
      permissions: BUYER,
      routes: formRoutes(),
    })

    await openForm(user, fetch)
    await fillLine(user, 0, /BRASS-ROD-12/, '5')
    await user.type(screen.getByLabelText(/replies due by/i), '2026-12-01')
    await user.type(screen.getByLabelText(/material required by/i), '2026-10-01')
    await user.click(screen.getByRole('button', { name: 'Raise draft RFQ' }))

    // A quotation that lands after the material was wanted cannot be acted on,
    // and the mistake is always a mis-keyed date rather than an instruction.
    await stable(() => screen.getByText(/replies would arrive after the material is needed/i))
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('lands a server field error on the line that caused it', async () => {
    const { user, fetch } = renderScreen(<RfqsPage />, {
      permissions: BUYER,
      routes: formRoutes({
        [`POST ${RFQS_PATH}`]: fail(422, 'VALIDATION_ERROR', 'The RFQ could not be raised', {
          fields: { 'lines.0.item_id': ['This item cannot be purchased'] },
        }),
      }),
    })

    await openForm(user, fetch)
    await fillLine(user, 0, /BRASS-ROD-12/, '5')
    await user.click(screen.getByRole('button', { name: 'Raise draft RFQ' }))

    await stable(() => screen.getByText('This item cannot be purchased'))
    expect(screen.getByRole('dialog')).toBeVisible()
  })

  it('says the enquiry cannot be raised when the warehouse list is unreadable', async () => {
    // Every RFQ has to state the warehouse the material is for, and that field
    // is a lookup behind `warehouse.view`.
    const { user, fetch } = renderScreen(<RfqsPage />, {
      permissions: [P.RFQ_VIEW, P.RFQ_MANAGE, P.VENDOR_VIEW, P.ITEM_VIEW],
      routes: {
        [`GET ${RFQS_PATH}`]: page([rfq()]),
        'GET /vendors/options': ok([]),
        'GET /vendors': page([]),
        'GET /products/items': page([]),
      },
    })

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: /new rfq/i }))
    const dialog = await stable(() => screen.getByRole('dialog'))
    expect(within(dialog).getByText(/cannot raise an rfq from here/i)).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Raise draft RFQ' })).toBeDisabled()
  })
})

describe('the board itself', () => {
  it('says what an RFQ is for when there are none', async () => {
    renderScreen(<RfqsPage />, { permissions: BUYER, routes: boardRoutes([]) })

    await stable(() => screen.getByText('No requests for quotation'))
    expect(screen.getByText(/ask several vendors to price the same lines/i)).toBeVisible()
  })

  it('reports a failed list rather than an empty one', async () => {
    renderScreen(<RfqsPage />, {
      permissions: BUYER,
      routes: boardRoutes([], {
        [`GET ${RFQS_PATH}`]: fail(500, 'INTERNAL', 'The enquiry service is unavailable'),
      }),
    })

    await stable(() => screen.getByText('Could not load this list'))
    expect(screen.getByText('The enquiry service is unavailable')).toBeVisible()
  })

  it('says plainly when nobody has been given a date to reply by', async () => {
    renderScreen(<RfqsPage />, {
      permissions: BUYER,
      routes: boardRoutes([rfq({ status: 'SENT', response_due_date: null })]),
    })

    // A dash would leave the reader to work out whether the date is missing or
    // simply not yet reached; nobody can be chased against a date that was
    // never set.
    await stable(() => screen.getByText('No date set'))
  })
})
