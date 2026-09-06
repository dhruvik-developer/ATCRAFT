/**
 * The purchase requisition queue, and the dialog that raises one.
 *
 * Two rights meet on this screen and they belong to two different people. The
 * requester raises, amends, submits, cancels and deletes - all
 * `purchase_requisition.create`. The approver decides -
 * `purchase_requisition.approve`, which the API additionally refuses to the
 * person who raised the document. Every gating test below renders the screen
 * twice rather than stubbing `can()`, because a control gated on a neighbouring
 * right passes any test that does the latter, and six such defects have already
 * shipped in this codebase.
 *
 * The other half of the file is what the form *sends*. A screen that renders
 * perfectly and posts the wrong shape earns a 422 the user cannot read, so the
 * multi-line payload is asserted key by key: decimal strings that stay strings,
 * absent optional fields sent as `null` rather than as `""`, and `line_number`
 * re-issued from the editor's own order so removing a line does not leave the
 * API a gap it stores verbatim.
 *
 * `TooltipProvider` wraps the screen because `app-layout` does: the purpose
 * column renders a Radix tooltip and `Tooltip.Root` throws outside a provider.
 * The wrapper reproduces the composition the route already has - it stubs
 * nothing the screen depends on.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { TooltipProvider } from '@/components/ui/overlays'
import { toDateInput } from '@/lib/format'
import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type FetchRecorder, type RouteTable } from '@/test/harness'

import type { RequisitionRow } from './procurement-types'
import RequisitionsPage from './requisitions-page'

/**
 * Wait for a control, then read it again.
 *
 * The warehouse and department lookups settle after the rows do, and the commit
 * that follows replaces the row's DOM. A node captured by `findBy*` before that
 * commit is detached by the time it is used, so a click on it lands nowhere and
 * the test fails for a reason that has nothing to do with the screen. Every
 * interaction below therefore re-reads its target immediately before using it.
 */
async function stable<T extends HTMLElement>(read: () => T): Promise<T> {
  await waitFor(read)
  return read()
}

/**
 * Wait until the screen has stopped fetching.
 *
 * The warehouse and department names come from reference lookups that settle
 * after the rows do, and the commit that adds them rebuilds the row's DOM. A
 * press that began before that commit finishes on a node no longer in the
 * document, and nothing happens at all - so every test that works a control
 * inside a row waits here first. Two polls with no new request means everything
 * the screen asked for has arrived and the rows have stopped moving.
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

const WAREHOUSE_ID = '01a00000-0000-7000-8000-00000000w001'
const ITEM_BRASS = '01a00000-0000-7000-8000-0000000item1'
const ITEM_STEEL = '01a00000-0000-7000-8000-0000000item2'
const REQUISITION_ID = '01a00000-0000-7000-8000-0000000000r1'
const APPROVE_PATH = `/procurement/purchase-requisitions/${REQUISITION_ID}/approve`
const REJECT_PATH = `/procurement/purchase-requisitions/${REQUISITION_ID}/reject`

function requisition(overrides: Partial<RequisitionRow> = {}): RequisitionRow {
  return {
    id: REQUISITION_ID,
    code: 'PR-0001',
    status: 'DRAFT',
    requisition_date: '2026-09-01',
    required_date: '2026-09-30',
    priority: 5,
    purpose: null,
    source_type: 'MANUAL',
    source_id: null,
    department_id: null,
    warehouse_id: WAREHOUSE_ID,
    requested_by_id: null,
    approved_by_id: null,
    approved_at: null,
    created_at: '2026-09-01T09:00:00Z',
    ...overrides,
  }
}

/** The draft as its detail endpoint returns it, with one line to carry through an amend. */
function requisitionDetail() {
  return ok({
    ...requisition({ status: 'DRAFT' }),
    notes: null,
    rejection_reason: null,
    lines: [
      {
        id: '01a00000-0000-7000-8000-0000000000l1',
        line_number: 1,
        item_id: ITEM_BRASS,
        quantity: '12.5000',
        estimated_unit_price: null,
        description: 'Brass rod 12mm',
        uom_id: '01a00000-0000-7000-8000-00000000uom1',
        required_date: null,
        suggested_vendor_id: null,
        notes: null,
        converted_quantity: '0.0000',
      },
    ],
  })
}

/** The three lookups the queue itself makes, plus the rows under test. */
function queueRoutes(rows: RequisitionRow[], extra: RouteTable = {}): RouteTable {
  return {
    'GET /procurement/purchase-requisitions': page(rows),
    'GET /warehouses/options': ok([
      { id: WAREHOUSE_ID, code: 'WH-MAIN', name: 'Main store', is_active: true },
    ]),
    'GET /organization/departments': page([]),
    ...extra,
  }
}

/** Everything the raise-a-requisition dialog reaches for on top of the queue. */
function formRoutes(extra: RouteTable = {}): RouteTable {
  return queueRoutes([], {
    'GET /warehouses': page([{ id: WAREHOUSE_ID, code: 'WH-MAIN', name: 'Main store' }]),
    'GET /products/items': page([
      { id: ITEM_BRASS, code: 'BRASS-ROD-12', name: 'Brass rod 12mm' },
      { id: ITEM_STEEL, code: 'SS-SHEET-2', name: 'Stainless sheet 2mm' },
    ]),
    ...extra,
  })
}

const screenUnderTest = (
  <TooltipProvider>
    <RequisitionsPage />
  </TooltipProvider>
)

type User = ReturnType<typeof renderScreen>['user']

/** Open a row's action menu and hand back the menu to assert against. */
async function openActions(user: User, fetch: FetchRecorder, code: string): Promise<HTMLElement> {
  await settled(fetch)
  await user.click(screen.getByRole('button', { name: `Actions for ${code}` }))
  return await stable(() => screen.getByRole('menu'))
}

describe('who is offered what', () => {
  it('offers Raise requisition only to a principal who may create one', async () => {
    renderScreen(screenUnderTest, {
      permissions: [P.PURCHASE_REQUISITION_VIEW],
      routes: queueRoutes([requisition()]),
    })

    await stable(() => screen.getByText('PR-0001'))
    // A viewer who cannot raise one must not be walked into a dialog whose save
    // the API will refuse.
    expect(screen.queryByRole('button', { name: /raise requisition/i })).not.toBeInTheDocument()
  })

  it('offers it once the create right is held', async () => {
    renderScreen(screenUnderTest, {
      permissions: [P.PURCHASE_REQUISITION_VIEW, P.PURCHASE_REQUISITION_CREATE],
      routes: queueRoutes([requisition()]),
    })

    expect(
      await stable(() => screen.getByRole('button', { name: /raise requisition/i })),
    ).toBeVisible()
  })

  it('keeps the decision off the requester’s menu', async () => {
    // The create right covers raising, amending, submitting, cancelling and
    // deleting - and emphatically not deciding. Approve and Reject are their own
    // endpoints behind their own right, so offering them here would put a
    // requester through a dialog the API answers with a 403.
    const { user, fetch } = renderScreen(screenUnderTest, {
      permissions: [P.PURCHASE_REQUISITION_VIEW, P.PURCHASE_REQUISITION_CREATE],
      routes: queueRoutes([requisition({ status: 'PENDING_APPROVAL' })]),
    })

    const menu = await openActions(user, fetch, 'PR-0001')
    expect(within(menu).getByRole('menuitem', { name: 'Cancel requisition' })).toBeVisible()
    expect(within(menu).queryByRole('menuitem', { name: 'Approve' })).not.toBeInTheDocument()
    expect(within(menu).queryByRole('menuitem', { name: 'Reject' })).not.toBeInTheDocument()
  })

  it('gives the approver the decision and none of the requester’s commands', async () => {
    const { user, fetch } = renderScreen(screenUnderTest, {
      permissions: [P.PURCHASE_REQUISITION_VIEW, P.PURCHASE_REQUISITION_APPROVE],
      routes: queueRoutes([requisition({ status: 'PENDING_APPROVAL' })]),
    })

    const menu = await openActions(user, fetch, 'PR-0001')
    expect(within(menu).getByRole('menuitem', { name: 'Approve' })).toBeVisible()
    expect(within(menu).getByRole('menuitem', { name: 'Reject' })).toBeVisible()
    // Withdrawing a document from the approval queue is the requester's command,
    // and the route that does it enforces the create right.
    expect(
      within(menu).queryByRole('menuitem', { name: 'Cancel requisition' }),
    ).not.toBeInTheDocument()
  })
})

describe('the state machine decides which commands exist', () => {
  it('offers Submit on a draft and never Approve, however many rights are held', async () => {
    // `REQUISITION_LIFECYCLE` has no DRAFT -> APPROVED edge. A menu that offered
    // one would put an approver through a confirmation that ends in a 422 and
    // leave the draft exactly where it was.
    const { user, fetch } = renderScreen(screenUnderTest, {
      permissions: [
        P.PURCHASE_REQUISITION_VIEW,
        P.PURCHASE_REQUISITION_CREATE,
        P.PURCHASE_REQUISITION_APPROVE,
      ],
      routes: queueRoutes([requisition({ status: 'DRAFT' })]),
    })

    const menu = await openActions(user, fetch, 'PR-0001')
    expect(within(menu).getByRole('menuitem', { name: 'Submit for approval' })).toBeVisible()
    expect(within(menu).getByRole('menuitem', { name: 'Edit draft' })).toBeVisible()
    expect(within(menu).getByRole('menuitem', { name: 'Delete draft' })).toBeVisible()
    expect(within(menu).queryByRole('menuitem', { name: 'Approve' })).not.toBeInTheDocument()
    expect(within(menu).queryByRole('menuitem', { name: 'Reject' })).not.toBeInTheDocument()
  })

  it('offers nothing at all on a converted requisition', async () => {
    // CONVERTED has an RFQ or an order against it: it cannot be amended,
    // decided, or cancelled without saying nothing about the commitment already
    // made to a vendor. An empty menu would be worse than no menu.
    renderScreen(screenUnderTest, {
      permissions: [
        P.PURCHASE_REQUISITION_VIEW,
        P.PURCHASE_REQUISITION_CREATE,
        P.PURCHASE_REQUISITION_APPROVE,
      ],
      routes: queueRoutes([requisition({ status: 'CONVERTED' })]),
    })

    await stable(() => screen.getByText('PR-0001'))
    expect(screen.queryByRole('button', { name: /^Actions for/ })).not.toBeInTheDocument()
  })

  it('lets an approved requisition be called off but not edited or deleted', async () => {
    const { user, fetch } = renderScreen(screenUnderTest, {
      permissions: [P.PURCHASE_REQUISITION_VIEW, P.PURCHASE_REQUISITION_CREATE],
      routes: queueRoutes([requisition({ status: 'APPROVED' })]),
    })

    // Approved demand may still be withdrawn, but the draft it once was is gone:
    // `update_requisition` and `delete_draft_requisition` both refuse anything
    // past DRAFT.
    const menu = await openActions(user, fetch, 'PR-0001')
    expect(within(menu).getByRole('menuitem', { name: 'Cancel requisition' })).toBeVisible()
    expect(within(menu).queryByRole('menuitem', { name: 'Edit draft' })).not.toBeInTheDocument()
    expect(within(menu).queryByRole('menuitem', { name: 'Delete draft' })).not.toBeInTheDocument()
  })
})

describe('what the decision sends', () => {
  const decisionRoutes = queueRoutes([requisition({ status: 'PENDING_APPROVAL' })], {
    [`POST ${APPROVE_PATH}`]: ok(requisition({ status: 'APPROVED' })),
    [`POST ${REJECT_PATH}`]: ok(requisition({ status: 'REJECTED' })),
  })

  it('refuses to send a rejection with no recorded reason', async () => {
    const { user, fetch } = renderScreen(screenUnderTest, {
      permissions: [P.PURCHASE_REQUISITION_VIEW, P.PURCHASE_REQUISITION_APPROVE],
      routes: decisionRoutes,
    })

    const menu = await openActions(user, fetch, 'PR-0001')
    await user.click(within(menu).getByRole('menuitem', { name: 'Reject' }))
    await user.click(await stable(() => screen.getByRole('button', { name: 'Reject requisition' })))

    expect(await stable(() => screen.getByRole('alert'))).toHaveTextContent(
      /at least three characters/i,
    )
    // The requester only ever sees this sentence, so an empty rejection reason
    // is a decision nobody can act on. It must not reach the API at all.
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('stops the rejection reason at the 255 characters the API accepts', async () => {
    // Past 255 the API answers a 422 naming the field, which the global toast
    // suppresses on the assumption the form shows it - so the control stops
    // the paste where it is typed rather than failing silently.
    const { user, fetch } = renderScreen(screenUnderTest, {
      permissions: [P.PURCHASE_REQUISITION_VIEW, P.PURCHASE_REQUISITION_APPROVE],
      routes: decisionRoutes,
    })

    const menu = await openActions(user, fetch, 'PR-0001')
    await user.click(within(menu).getByRole('menuitem', { name: 'Reject' }))
    const reasonBox = await stable(() => screen.getByRole('textbox', { name: 'Reason' }))
    expect(reasonBox).toHaveAttribute('maxlength', '255')
  })

  it('posts the rejection reason as the API records it', async () => {
    const { user, fetch } = renderScreen(screenUnderTest, {
      permissions: [P.PURCHASE_REQUISITION_VIEW, P.PURCHASE_REQUISITION_APPROVE],
      routes: decisionRoutes,
    })

    const menu = await openActions(user, fetch, 'PR-0001')
    await user.click(within(menu).getByRole('menuitem', { name: 'Reject' }))
    await user.type(
      await stable(() => screen.getByRole('textbox', { name: 'Reason' })),
      '  Budget withdrawn  ',
    )
    await user.click(screen.getByRole('button', { name: 'Reject requisition' }))

    await waitFor(() => fetch.requestFor('POST', REJECT_PATH))
    // Trimmed, because what is recorded here is shown back to the requester
    // verbatim as the reason their demand was refused.
    expect(fetch.requestFor('POST', REJECT_PATH)).toEqual({ reason: 'Budget withdrawn' })
  })

  it('sends null rather than an empty string for an approval with no note', async () => {
    const { user, fetch } = renderScreen(screenUnderTest, {
      permissions: [P.PURCHASE_REQUISITION_VIEW, P.PURCHASE_REQUISITION_APPROVE],
      routes: decisionRoutes,
    })

    const menu = await openActions(user, fetch, 'PR-0001')
    await user.click(within(menu).getByRole('menuitem', { name: 'Approve' }))
    await user.click(
      await stable(() => screen.getByRole('button', { name: 'Approve requisition' })),
    )

    await waitFor(() => fetch.requestFor('POST', APPROVE_PATH))
    // `ApproveRequest` reads null as "no note recorded" and writes its own
    // sentence in its place. An empty string is a note that says nothing.
    expect(fetch.requestFor('POST', APPROVE_PATH)).toEqual({ reason: null })
  })

  it('sends the delete reason as a query parameter, not a body', async () => {
    // `DELETE /purchase-requisitions/{id}` takes its reason in the query string.
    // A body on a DELETE never arrives, and the audit row - the only trace a
    // deleted draft leaves - would be the one thing lost.
    let reasonParam: string | null = 'never called'
    const { user, fetch } = renderScreen(screenUnderTest, {
      permissions: [P.PURCHASE_REQUISITION_VIEW, P.PURCHASE_REQUISITION_CREATE],
      routes: queueRoutes([requisition({ status: 'DRAFT' })], {
        'DELETE /procurement/purchase-requisitions': ({ url }) => {
          reasonParam = url.searchParams.get('reason')
          return ok(null)
        },
      }),
    })

    const menu = await openActions(user, fetch, 'PR-0001')
    await user.click(within(menu).getByRole('menuitem', { name: 'Delete draft' }))
    await user.type(
      await stable(() => screen.getByRole('textbox', { name: 'Reason' })),
      'Raised in error',
    )
    await user.click(screen.getByRole('button', { name: 'Delete draft' }))

    await waitFor(() => expect(reasonParam).toBe('Raised in error'))
    expect(fetch.calls.find((entry) => entry.method === 'DELETE')?.body).toBeUndefined()
  })
})

describe('the requisition form', () => {
  const formPermissions = [
    P.PURCHASE_REQUISITION_VIEW,
    P.PURCHASE_REQUISITION_CREATE,
    P.WAREHOUSE_VIEW,
    P.ITEM_VIEW,
  ]

  /** Open the dialog and fill the header fields the schema demands. */
  async function openForm(user: User, fetch: FetchRecorder) {
    await settled(fetch)
    await user.click(screen.getByRole('button', { name: /raise requisition/i }))
    await stable(() => screen.getByRole('dialog'))
    await user.type(screen.getByLabelText(/required by/i), '2026-10-15')
    await user.click(screen.getByRole('combobox', { name: 'Needed at' }))
    await user.click(await stable(() => screen.getByRole('option', { name: /WH-MAIN/ })))
  }

  /** Choose an item and key a quantity on one line of the editor. */
  async function fillLine(user: User, index: number, itemName: RegExp, quantity: string) {
    await user.click(screen.getByRole('combobox', { name: `Line ${index + 1} item` }))
    await user.click(await stable(() => screen.getByRole('option', { name: itemName })))
    await user.type(screen.getAllByRole('textbox', { name: 'Quantity' })[index]!, quantity)
  }

  it('sends two lines numbered from the editor, with the decimals still strings', async () => {
    const { user, fetch } = renderScreen(screenUnderTest, {
      permissions: formPermissions,
      routes: formRoutes({
        'POST /procurement/purchase-requisitions': ok({ code: 'PR-0002', lines: [{}, {}] }),
      }),
    })

    await openForm(user, fetch)
    await fillLine(user, 0, /BRASS-ROD-12/, '12.5')

    await user.click(screen.getByRole('button', { name: /add line/i }))
    await fillLine(user, 1, /SS-SHEET-2/, '3')
    await user.type(screen.getAllByRole('textbox', { name: 'Estimated price' })[1]!, '145.500000')

    await user.click(screen.getByRole('button', { name: 'Raise draft requisition' }))

    await waitFor(() => fetch.requestFor('POST', '/procurement/purchase-requisitions'))
    const body = fetch.requestFor('POST', '/procurement/purchase-requisitions') as {
      source_type: string
      warehouse_id: string
      requisition_date: string
      required_date: string
      department_id: unknown
      purpose: unknown
      notes: unknown
      priority: number
      lines: Record<string, unknown>[]
    }

    expect(body.warehouse_id).toBe(WAREHOUSE_ID)
    expect(body.required_date).toBe('2026-10-15')
    expect(body.requisition_date).toBe(toDateInput(new Date()))
    // This screen only ever raises demand by hand; a converted MRP suggestion
    // arrives through the planner carrying its own source type and source id.
    expect(body.source_type).toBe('MANUAL')
    // Absent optional text is null, never "": `update_requisition` skips a null
    // but would store an empty string as a purpose that says nothing.
    expect(body.department_id).toBeNull()
    expect(body.purpose).toBeNull()
    expect(body.notes).toBeNull()
    // A number, not a decimal string: priority is an integer 1-9 on the wire,
    // unlike every quantity and price around it.
    expect(body.priority).toBe(5)

    expect(body.lines).toHaveLength(2)
    expect(body.lines[0]).toEqual({
      line_number: 1,
      item_id: ITEM_BRASS,
      quantity: '12.5',
      estimated_unit_price: null,
      description: null,
      // Left to the API, which fills in the item's own order unit - a better
      // answer than one this form is in any position to invent.
      uom_id: null,
      required_date: null,
      suggested_vendor_id: null,
      notes: null,
    })
    expect(body.lines[1]).toMatchObject({
      line_number: 2,
      item_id: ITEM_STEEL,
      quantity: '3',
      // Six decimal places survive intact. Through a float this is 145.5, and
      // the API's NUMERIC(18,6) silently keeps whatever arrives.
      estimated_unit_price: '145.500000',
    })
    expect(typeof body.lines[0]!.quantity).toBe('string')
    expect(typeof body.lines[1]!.estimated_unit_price).toBe('string')
  })

  it('renumbers the line that survives a removal', async () => {
    const { user, fetch } = renderScreen(screenUnderTest, {
      permissions: formPermissions,
      routes: formRoutes({
        'POST /procurement/purchase-requisitions': ok({ code: 'PR-0003', lines: [{}] }),
      }),
    })

    await openForm(user, fetch)
    await fillLine(user, 0, /BRASS-ROD-12/, '1')
    await user.click(screen.getByRole('button', { name: /add line/i }))
    await fillLine(user, 1, /SS-SHEET-2/, '2')

    await user.click(screen.getByRole('button', { name: 'Remove line 1' }))
    await user.click(screen.getByRole('button', { name: 'Raise draft requisition' }))

    await waitFor(() => fetch.requestFor('POST', '/procurement/purchase-requisitions'))
    const body = fetch.requestFor('POST', '/procurement/purchase-requisitions') as {
      lines: Record<string, unknown>[]
    }
    // The survivor takes number 1. Sending it as line 2 would leave the API a
    // gap it stores verbatim, and the document would read wrong for good.
    expect(body.lines).toEqual([
      expect.objectContaining({ line_number: 1, item_id: ITEM_STEEL, quantity: '2' }),
    ])
  })

  it('will not let the last line be removed', async () => {
    const { user, fetch } = renderScreen(screenUnderTest, {
      permissions: formPermissions,
      routes: formRoutes(),
    })

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: /raise requisition/i }))
    await stable(() => screen.getByRole('dialog'))
    // `RequisitionCreate` declares `lines` as required and refuses an empty set,
    // so a form that could be emptied would offer a save that cannot succeed.
    expect(screen.getByRole('button', { name: 'Remove line 1' })).toBeDisabled()
  })

  it('lands a server field error on the line that caused it', async () => {
    const { user, fetch } = renderScreen(screenUnderTest, {
      permissions: formPermissions,
      routes: formRoutes({
        'POST /procurement/purchase-requisitions': fail(
          422,
          'VALIDATION_ERROR',
          'The requisition could not be saved',
          { fields: { 'lines.0.quantity': ['Exceeds the maximum order quantity for this item'] } },
        ),
      }),
    })

    await openForm(user, fetch)
    await fillLine(user, 0, /BRASS-ROD-12/, '99999')
    await user.click(screen.getByRole('button', { name: 'Raise draft requisition' }))

    // The API addresses field errors by the dotted path react-hook-form uses, so
    // a line-level refusal belongs on that line rather than in a toast the
    // requester has to map back onto the sheet themselves.
    await stable(() => screen.getByText('Exceeds the maximum order quantity for this item'))
    expect(screen.getByRole('dialog')).toBeVisible()
  })

  it('does not offer a blank form to save over a draft that failed to load', async () => {
    // `RequisitionUpdate.lines` replaces the whole set. A blank form after a
    // failed load is not an empty requisition: saving it would delete every
    // line the draft has. So nothing editable, and no Save, until it loads.
    let attempts = 0
    const { user, fetch } = renderScreen(screenUnderTest, {
      permissions: formPermissions,
      routes: formRoutes({
        'GET /procurement/purchase-requisitions': page([requisition({ status: 'DRAFT' })]),
        [`GET /procurement/purchase-requisitions/${REQUISITION_ID}`]: () => {
          attempts += 1
          return attempts === 1
            ? fail(500, 'INTERNAL', 'The requisition could not be read')
            : requisitionDetail()
        },
        // The line's item is resolved by id so the picker reads as a code.
        [`GET /products/items/${ITEM_BRASS}`]: ok({
          id: ITEM_BRASS,
          code: 'BRASS-ROD-12',
          name: 'Brass rod 12mm',
        }),
      }),
    })

    const menu = await openActions(user, fetch, 'PR-0001')
    await user.click(within(menu).getByRole('menuitem', { name: 'Edit draft' }))
    const dialog = await stable(() => screen.getByRole('dialog'))

    await stable(() => within(dialog).getByText('Could not load this requisition'))
    expect(within(dialog).getByText('The requisition could not be read')).toBeVisible()
    expect(within(dialog).queryByRole('button', { name: 'Save draft' })).not.toBeInTheDocument()
    expect(within(dialog).queryByRole('textbox', { name: 'Quantity' })).not.toBeInTheDocument()

    // Try again brings the draft, and only then the form - with its lines.
    await user.click(within(dialog).getByRole('button', { name: /try again/i }))
    await stable(() => screen.getByRole('button', { name: 'Save draft' }))
    expect(screen.getByRole('textbox', { name: 'Quantity' })).toHaveValue('12.5000')
  })

  it('says why it cannot be saved when the warehouse list is unreadable', async () => {
    // Every requisition has to state where the material is wanted, and that
    // field is a lookup behind `warehouse.view`. Without it there is nothing to
    // choose, so the form says so rather than failing on a field never shown.
    const { user, fetch } = renderScreen(screenUnderTest, {
      permissions: [P.PURCHASE_REQUISITION_VIEW, P.PURCHASE_REQUISITION_CREATE, P.ITEM_VIEW],
      routes: queueRoutes([], { 'GET /products/items': page([]) }),
    })

    await settled(fetch)
    await user.click(screen.getByRole('button', { name: /raise requisition/i }))
    const dialog = await stable(() => screen.getByRole('dialog'))
    expect(within(dialog).getByText(/cannot save a requisition from here/i)).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Raise draft requisition' })).toBeDisabled()
  })
})

describe('the queue itself', () => {
  it('says what to do when nothing matches', async () => {
    renderScreen(screenUnderTest, {
      permissions: [P.PURCHASE_REQUISITION_VIEW],
      routes: queueRoutes([]),
    })

    await stable(() => screen.getByText('No requisitions found'))
    expect(screen.getByText(/widen the date window or clear a filter/i)).toBeVisible()
  })

  it('reports a failed list rather than an empty one', async () => {
    // An empty table and a broken query look identical to a buyer, and the
    // difference is whether there is no work or the screen is lying about it.
    renderScreen(screenUnderTest, {
      permissions: [P.PURCHASE_REQUISITION_VIEW],
      routes: queueRoutes([], {
        'GET /procurement/purchase-requisitions': fail(500, 'INTERNAL', 'Upstream unavailable'),
      }),
    })

    await stable(() => screen.getByText('Could not load this list'))
    expect(screen.getByRole('button', { name: /try again/i })).toBeVisible()
  })

  it('flags a late requisition that somebody can still act on', async () => {
    renderScreen(screenUnderTest, {
      permissions: [P.PURCHASE_REQUISITION_VIEW],
      routes: queueRoutes([
        requisition({
          status: 'APPROVED',
          required_date: '2020-01-01',
          purpose: 'Rebuild the press tooling',
        }),
      ]),
    })

    // Only an undecided or unsourced requisition can be late in a way anybody
    // can do something about; on a closed one the flag is noise.
    await stable(() => screen.getByText(/d late\)$/))
    expect(screen.getByText('Rebuild the press tooling')).toBeVisible()
  })

  it('does not flag a late date on a requisition that is already closed', async () => {
    renderScreen(screenUnderTest, {
      permissions: [P.PURCHASE_REQUISITION_VIEW],
      routes: queueRoutes([requisition({ status: 'CANCELLED', required_date: '2020-01-01' })]),
    })

    await stable(() => screen.getByText('PR-0001'))
    expect(screen.queryByText(/d late\)$/)).not.toBeInTheDocument()
  })
})
