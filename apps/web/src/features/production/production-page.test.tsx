/**
 * Production orders: the register, and the two dialogs that actually move
 * material.
 *
 * The register is the cheap half. The dialogs are why this file is long: an
 * issue and a return are each **two** requests - the picking document is drafted
 * and then posted, and nothing leaves the store until the second one lands - and
 * a return is bounded by what the job is still holding *less what it has already
 * consumed*. The API's own ceiling is the holding alone, so a form that offers
 * more than `net_issued - consumed` posts a return the server accepts and which
 * silently un-consumes material that was really built into the product.
 *
 * Everything a form sends is asserted as the literal body, because a screen that
 * renders correctly and posts the wrong shape is exactly the defect this suite
 * exists to catch - a quantity that became a JS number on the way to the wire
 * looks identical on screen.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen } from '@/test/harness'

import {
  IssueMaterialDialog,
  ProductionOrderFormDialog,
  ReturnMaterialDialog,
} from './production-dialogs'
import type { ProductionComponent, ProductionOrderRow } from './production-model'
import ProductionPage from './production-page'

// ------------------------------------------------------------- fixtures ----

function order(overrides: Partial<ProductionOrderRow> = {}): ProductionOrderRow {
  return {
    id: 'po-1',
    code: 'PRD-0001',
    status: 'RELEASED',
    item_id: 'item-1',
    warehouse_id: 'wh-1',
    plant_id: null,
    cost_centre_id: null,
    planned_quantity: '100.0000',
    produced_quantity: '25.0000',
    rejected_quantity: '0.0000',
    scrap_quantity: '0.0000',
    planned_start_date: '2026-01-05',
    planned_end_date: '2026-01-09',
    actual_start_date: null,
    actual_end_date: null,
    priority: 100,
    source_type: 'MANUAL',
    source_id: null,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function component(overrides: Partial<ProductionComponent> = {}): ProductionComponent {
  return {
    id: 'comp-1',
    production_order_id: 'po-1',
    line_number: 1,
    item_id: 'item-9',
    item_code: 'BRASS-ROD-12',
    uom_id: 'uom-1',
    warehouse_id: 'wh-1',
    bom_level: 1,
    required_quantity: '100.0000',
    issued_quantity: '40.0000',
    returned_quantity: '0.0000',
    net_issued_quantity: '40.0000',
    consumed_quantity: '0.0000',
    is_backflushed: false,
    ...overrides,
  }
}

/** The warehouse register both dialogs read for their "issue from" select. */
const WAREHOUSES = { 'GET /warehouses': page([{ id: 'wh-1', code: 'MAIN', name: 'Main store' }]) }

/** A recorded request body, read once through a cast rather than ten times. */
function sentBody(value: unknown): Record<string, unknown> {
  return value as Record<string, unknown>
}

/** The `lines` array of a recorded body, which every assertion here reaches into. */
function sentLines(value: unknown): Record<string, unknown>[] {
  return (value as { lines: Record<string, unknown>[] }).lines
}

// ------------------------------------------------------------- the list -----

describe('the production register', () => {
  it('offers "New order" only to someone who may raise one', async () => {
    const listed = { 'GET /production': page([order()]) }

    const withRight = renderScreen(<ProductionPage />, {
      permissions: [P.PRODUCTION_ORDER_VIEW, P.PRODUCTION_ORDER_CREATE],
      routes: listed,
    })
    expect(await screen.findByRole('button', { name: /new order/i })).toBeVisible()
    withRight.unmount()

    // The same screen, one right short. Six authorisation defects have shipped
    // in this codebase and every one was a control gated on the wrong right, so
    // the negative half is the half that matters.
    renderScreen(<ProductionPage />, {
      permissions: [P.PRODUCTION_ORDER_VIEW],
      routes: listed,
    })
    expect(await screen.findByText('PRD-0001')).toBeVisible()
    expect(screen.queryByRole('button', { name: /new order/i })).toBeNull()
  })

  it('does not offer the way out of an empty list to someone who cannot take it', async () => {
    // The empty state repeats the create button. Repeating it *ungated* is how a
    // read-only planner ends up in a form the API will refuse.
    renderScreen(<ProductionPage />, {
      permissions: [P.PRODUCTION_ORDER_VIEW],
      routes: { 'GET /production': page([]) },
    })
    expect(await screen.findByText('No production orders found')).toBeVisible()
    expect(screen.queryByRole('button', { name: /new order/i })).toBeNull()
  })

  it('does not claim the list is empty while it is still loading', async () => {
    // "No production orders found" on a list that is merely in flight reads as
    // an answer. The table must show its skeleton until the server has spoken.
    renderScreen(<ProductionPage />, {
      permissions: [P.PRODUCTION_ORDER_VIEW],
      routes: { 'GET /production': page([]) },
    })
    expect(screen.queryByText('No production orders found')).toBeNull()
    expect(await screen.findByText('No production orders found')).toBeVisible()
  })

  it('sends the date window as planned_from/planned_to, the names the API answers', async () => {
    // An unknown query parameter is dropped without complaint, so under
    // date_from/date_to the window a planner typed changed nothing at all and
    // the list looked merely wrong rather than broken.
    const seen: { url?: URL } = {}
    renderScreen(<ProductionPage />, {
      permissions: [P.PRODUCTION_ORDER_VIEW],
      route: '/production?status=RELEASED&planned_from=2026-01-01&planned_to=2026-01-31',
      routes: {
        'GET /production': ({ url }) => {
          seen.url = url
          return page([order()])
        },
      },
    })
    await screen.findByText('PRD-0001')

    const sent = seen.url!.searchParams
    expect(sent.get('status')).toBe('RELEASED')
    expect(sent.get('planned_from')).toBe('2026-01-01')
    expect(sent.get('planned_to')).toBe('2026-01-31')
    expect(sent.get('date_from')).toBeNull()
    expect(sent.get('date_to')).toBeNull()
  })

  it('reports progress as a decimal division and says the number out loud', async () => {
    // Colour alone is not an answer to "how far along is this". The value is
    // also what a 1/3 division has to survive: through IEEE-754 the bar reads
    // 33.33333333333333 rather than 33.3.
    renderScreen(<ProductionPage />, {
      permissions: [P.PRODUCTION_ORDER_VIEW],
      routes: {
        'GET /production': page([
          order({ planned_quantity: '3.0000', produced_quantity: '1.0000' }),
        ]),
      },
    })
    const bar = await screen.findByRole('progressbar', { name: 'PRD-0001 progress' })
    expect(bar).toHaveAttribute('aria-valuenow', '33.3')
    expect(screen.getByText('33.3%')).toBeVisible()
  })

  it('tells the user a refused list was refused instead of showing it as empty', async () => {
    renderScreen(<ProductionPage />, {
      permissions: [P.PRODUCTION_ORDER_VIEW],
      routes: {
        'GET /production': fail(403, 'PERMISSION_DENIED', 'Production orders are not yours to see'),
      },
    })
    expect(await screen.findByText('Could not load this list')).toBeVisible()
    expect(screen.getByText('Production orders are not yours to see')).toBeVisible()
    // The empty state would be a lie here: there may well be orders.
    expect(screen.queryByText('No production orders found')).toBeNull()
  })
})

// ------------------------------------------------------- raising an order ----

describe('raising an order', () => {
  /**
   * The cost centre is explicit or absent - an order inherits none - and the
   * material issued to it is charged there through the order. The picker is
   * its own right (`cost_centre.view`); without it the order is raised
   * unallocated rather than behind a list that could only be refused.
   */
  const ITEM = { id: 'item-hdl', code: 'HDL-200', name: 'Pull handle 200mm', is_active: true }
  const CENTRES = [
    {
      id: 'cc-mach',
      code: 'CC-MACH',
      name: 'Machining',
      cost_centre_type: 'PRODUCTION',
      parent_id: null,
      plant_id: null,
      head_user_id: null,
      description: null,
      is_active: true,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
  ]
  const routes = {
    'GET /products/items': page([ITEM]),
    'GET /bom': page([]),
    ...WAREHOUSES,
    'POST /production': ok(order({ id: 'po-9', code: 'PRD-0009', status: 'DRAFT' })),
  }

  async function fillTheMinimum(
    user: ReturnType<typeof renderScreen>['user'],
    dialog: HTMLElement,
  ) {
    await user.click(within(dialog).getByRole('combobox', { name: 'Item to make' }))
    await user.click(await screen.findByRole('option', { name: /HDL-200/ }))
    await user.click(within(dialog).getByRole('combobox', { name: 'Receive output into' }))
    await user.click(await screen.findByRole('option', { name: /MAIN/ }))
  }

  it('charges the order to the centre chosen, for an account that may read centres', async () => {
    const { user, fetch } = renderScreen(
      <ProductionOrderFormDialog open onOpenChange={() => {}} />,
      {
        permissions: [P.PRODUCTION_ORDER_CREATE, P.COST_CENTRE_VIEW],
        routes: { ...routes, 'GET /organization/cost-centres': page(CENTRES) },
      },
    )

    const dialog = await screen.findByRole('dialog')
    await fillTheMinimum(user, dialog)
    await user.click(within(dialog).getByRole('combobox', { name: 'Cost centre' }))
    await user.click(await screen.findByRole('option', { name: 'CC-MACH - Machining' }))
    await user.click(within(dialog).getByRole('button', { name: /raise order/i }))

    const body = sentBody(await waitFor(() => fetch.requestFor('POST', '/production')))
    expect(body.cost_centre_id).toBe('cc-mach')
    expect(body.item_id).toBe(ITEM.id)
  })

  it('raises it unallocated, asking for no centre, without cost_centre.view', async () => {
    const { user, fetch } = renderScreen(
      <ProductionOrderFormDialog open onOpenChange={() => {}} />,
      {
        permissions: [P.PRODUCTION_ORDER_CREATE],
        routes,
      },
    )

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).queryByRole('combobox', { name: 'Cost centre' })).not.toBeInTheDocument()
    await fillTheMinimum(user, dialog)
    await user.click(within(dialog).getByRole('button', { name: /raise order/i }))

    const body = sentBody(await waitFor(() => fetch.requestFor('POST', '/production')))
    expect(body.cost_centre_id).toBeNull()
    expect(fetch.calls.some((call) => call.path.startsWith('/organization/cost-centres'))).toBe(
      false,
    )
  })
})

// -------------------------------------------------------- issue material ----

describe('issuing material', () => {
  it('pre-fills each line with what is still outstanding, net of returns', async () => {
    // Required less *net* issued: material drawn and handed back is not on the
    // floor and has to be drawn again. Pre-filling with `required - issued`
    // would under-issue every line that has ever been returned against.
    renderScreen(
      <IssueMaterialDialog
        open
        onOpenChange={() => {}}
        orderId="po-1"
        orderCode="PRD-0001"
        components={[
          component({
            required_quantity: '100',
            issued_quantity: '55',
            returned_quantity: '15',
            net_issued_quantity: '40',
          }),
          component({
            id: 'comp-2',
            line_number: 2,
            item_code: 'STEEL-PIN-4',
            required_quantity: '10',
            net_issued_quantity: '10',
          }),
        ]}
      />,
      { permissions: [P.MATERIAL_ISSUE_CREATE], routes: WAREHOUSES },
    )

    expect(await screen.findByLabelText('BRASS-ROD-12')).toHaveValue('60')
    // Fully kitted already: offered as zero rather than silently re-issued.
    expect(screen.getByLabelText('STEEL-PIN-4')).toHaveValue('0')
  })

  it('drafts the picking document and then posts it, because the draft moves nothing', async () => {
    const { user, fetch } = renderScreen(
      <IssueMaterialDialog
        open
        onOpenChange={() => {}}
        orderId="po-1"
        orderCode="PRD-0001"
        components={[
          component(),
          component({
            id: 'comp-2',
            line_number: 2,
            item_code: 'STEEL-PIN-4',
            required_quantity: '10',
            net_issued_quantity: '10',
          }),
        ]}
      />,
      {
        permissions: [P.MATERIAL_ISSUE_CREATE],
        routes: {
          ...WAREHOUSES,
          'POST /production/po-1/material-issues': ok({ id: 'mi-9', code: 'MI-0007' }),
          'POST /production/material-issues/mi-9/post': ok({
            message: 'Posted',
            issue: { code: 'MI-0007', total_quantity: '60.0000', line_count: 1 },
            order_status: 'MATERIAL_ISSUED',
          }),
        },
      },
    )

    await screen.findByLabelText('BRASS-ROD-12')
    await user.click(screen.getByRole('button', { name: 'Issue material' }))

    await waitFor(() => fetch.requestFor('POST', '/production/material-issues/mi-9/post'))

    const draft = sentBody(fetch.requestFor('POST', '/production/po-1/material-issues'))
    // No warehouse chosen means the component's own store, which the contract
    // spells `null` - not `""`, which is a uuid the API cannot parse.
    expect(draft.warehouse_id).toBeNull()
    expect(draft.reason).toBe('Material issue to production order')
    // A line at zero is left for later rather than posted as a no-op movement,
    // and the line is keyed by its number because a kit list may legitimately
    // carry the same part twice.
    expect(draft.lines).toEqual([{ component_id: 'comp-1', line_number: 1, quantity: '60' }])
    // Decimal strings stay strings all the way to the wire.
    expect(typeof sentLines(draft)[0]!.quantity).toBe('string')
    // The posting body is required and empty: `allow_partial_issue` is a
    // supervisor's decision, not a side effect of this dialog.
    expect(sentBody(fetch.requestFor('POST', '/production/material-issues/mi-9/post'))).toEqual({})
  })

  it('refuses to submit with nothing keyed, and says so where the operator is looking', async () => {
    const { user, fetch } = renderScreen(
      <IssueMaterialDialog
        open
        onOpenChange={() => {}}
        orderId="po-1"
        orderCode="PRD-0001"
        components={[component({ required_quantity: '40', net_issued_quantity: '40' })]}
      />,
      { permissions: [P.MATERIAL_ISSUE_CREATE], routes: WAREHOUSES },
    )

    await screen.findByLabelText('BRASS-ROD-12')
    await user.click(screen.getByRole('button', { name: 'Issue material' }))

    expect(await screen.findByText('Enter a quantity against at least one component')).toBeVisible()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })

  it('lands a 422 on the line the server named', async () => {
    // The API addresses a field-array entry as `lines.0.quantity`, which is
    // exactly how react-hook-form addresses it. If that mapping breaks the
    // message goes nowhere and the form merely refuses, in silence.
    const { user } = renderScreen(
      <IssueMaterialDialog
        open
        onOpenChange={() => {}}
        orderId="po-1"
        orderCode="PRD-0001"
        components={[component()]}
      />,
      {
        permissions: [P.MATERIAL_ISSUE_CREATE],
        routes: {
          ...WAREHOUSES,
          'POST /production/po-1/material-issues': fail(422, 'VALIDATION_ERROR', 'Invalid', {
            fields: { 'lines.0.quantity': ['Only 12 available in MAIN'] },
          }),
        },
      },
    )

    await screen.findByLabelText('BRASS-ROD-12')
    await user.click(screen.getByRole('button', { name: 'Issue material' }))

    expect(await screen.findByText('Only 12 available in MAIN')).toBeVisible()
  })

  it('names the stranded draft when the posting fails, so the kit is not drafted twice', async () => {
    // The dangerous half of a two-call write: the document exists, no stock
    // moved, and resubmitting the form drafts a second identical picking list.
    const { user } = renderScreen(
      <IssueMaterialDialog
        open
        onOpenChange={() => {}}
        orderId="po-1"
        orderCode="PRD-0001"
        components={[component()]}
      />,
      {
        permissions: [P.MATERIAL_ISSUE_CREATE],
        routes: {
          ...WAREHOUSES,
          'POST /production/po-1/material-issues': ok({ id: 'mi-9', code: 'MI-0007' }),
          'POST /production/material-issues/mi-9/post': fail(
            409,
            'INSUFFICIENT_AVAILABLE_STOCK',
            'Not enough free stock to post this issue',
          ),
        },
      },
    )

    await screen.findByLabelText('BRASS-ROD-12')
    await user.click(screen.getByRole('button', { name: 'Issue material' }))

    expect(await screen.findByText(/Picking document MI-0007 was created/)).toBeVisible()
  })
})

// ------------------------------------------------------- return material ----

describe('returning material', () => {
  const holding = () => component({ net_issued_quantity: '50.0000', consumed_quantity: '30.0000' })

  it('defaults to issued less consumed, not to the whole holding', async () => {
    // The API's own ceiling is the holding alone and it then trims
    // `consumed_quantity` down to whatever is left - so a return keyed at 50
    // would be accepted and would un-consume 30 units that were really built
    // into the product, leaving the costing describing something nobody made.
    renderScreen(
      <ReturnMaterialDialog
        open
        onOpenChange={() => {}}
        orderId="po-1"
        orderCode="PRD-0001"
        components={[holding()]}
      />,
      { permissions: [P.MATERIAL_RETURN_CREATE], routes: WAREHOUSES },
    )

    expect(await screen.findByLabelText('BRASS-ROD-12')).toHaveValue('20')
  })

  it('never offers more than that, and stops the whole document rather than one line', async () => {
    // The service refuses the entire return on the first over-quantity, so a
    // slip on one row would throw away every other line the storekeeper had
    // just counted. The rule is therefore held here, per row, before the post.
    const { user, fetch } = renderScreen(
      <ReturnMaterialDialog
        open
        onOpenChange={() => {}}
        orderId="po-1"
        orderCode="PRD-0001"
        components={[holding()]}
      />,
      { permissions: [P.MATERIAL_RETURN_CREATE], routes: WAREHOUSES },
    )

    const field = await screen.findByLabelText('BRASS-ROD-12')
    await user.clear(field)
    await user.type(field, '21')
    await user.click(screen.getByRole('button', { name: 'Return material' }))

    expect(await screen.findByText('At most 20 can be handed back')).toBeVisible()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })

  it('leaves out a component the job is no longer holding anything of', async () => {
    // A row that can return nothing is a row that can only be got wrong.
    renderScreen(
      <ReturnMaterialDialog
        open
        onOpenChange={() => {}}
        orderId="po-1"
        orderCode="PRD-0001"
        components={[
          holding(),
          component({
            id: 'comp-2',
            line_number: 2,
            item_code: 'STEEL-PIN-4',
            net_issued_quantity: '10.0000',
            consumed_quantity: '10.0000',
          }),
        ]}
      />,
      { permissions: [P.MATERIAL_RETURN_CREATE], routes: WAREHOUSES },
    )

    expect(await screen.findByLabelText('BRASS-ROD-12')).toBeVisible()
    expect(screen.queryByLabelText('STEEL-PIN-4')).toBeNull()
  })

  it('says so, and disables the post, when the job is holding nothing at all', async () => {
    renderScreen(
      <ReturnMaterialDialog
        open
        onOpenChange={() => {}}
        orderId="po-1"
        orderCode="PRD-0001"
        components={[component({ net_issued_quantity: '0.0000', consumed_quantity: '0.0000' })]}
      />,
      { permissions: [P.MATERIAL_RETURN_CREATE], routes: WAREHOUSES },
    )

    expect(
      await screen.findByText(
        'This order is not holding any material, so there is nothing to return.',
      ),
    ).toBeVisible()
    expect(screen.getByRole('button', { name: 'Return material' })).toBeDisabled()
  })

  it('posts nothing until the ledger warning is confirmed, then drafts a RETURN', async () => {
    const { user, fetch } = renderScreen(
      <ReturnMaterialDialog
        open
        onOpenChange={() => {}}
        orderId="po-1"
        orderCode="PRD-0001"
        components={[holding()]}
      />,
      {
        permissions: [P.MATERIAL_RETURN_CREATE],
        routes: {
          ...WAREHOUSES,
          'POST /production/po-1/material-returns': ok({ id: 'mr-3', code: 'MR-0002' }),
          'POST /production/material-issues/mr-3/post': ok({
            message: 'Posted',
            issue: { code: 'MR-0002', total_quantity: '20.0000', line_count: 1 },
            order_status: 'IN_PROCESS',
          }),
        },
      },
    )

    await screen.findByLabelText('BRASS-ROD-12')
    await user.click(screen.getByRole('button', { name: 'Return material' }))

    // A ledger row cannot be deleted, so the submit opens a confirmation rather
    // than posting.
    expect(await screen.findByRole('button', { name: 'Post return' })).toBeVisible()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)

    await user.click(screen.getByRole('button', { name: 'Post return' }))
    await waitFor(() => fetch.requestFor('POST', '/production/material-issues/mr-3/post'))

    // The draft goes to `/material-returns`. Sending it to `/material-issues`
    // would draft the mirror document and take the material out again.
    const draft = sentBody(fetch.requestFor('POST', '/production/po-1/material-returns'))
    expect(draft.warehouse_id).toBeNull()
    expect(draft.reason).toBe('Unused material returned to store')
    expect(draft.lines).toEqual([{ component_id: 'comp-1', line_number: 1, quantity: '20' }])
    expect(typeof sentLines(draft)[0]!.quantity).toBe('string')
  })
})
