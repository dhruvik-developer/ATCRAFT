/**
 * Raising purchase orders from approved requisitions.
 *
 * The API makes every decision in the split - vendor, price, category, which
 * order a line joins and who at the vendor it goes to - so what is worth a test
 * here is what the dialog *sends* and what it lets the buyer do with the answer:
 *
 * * every correction is a new preview carrying exactly that override, with the
 *   decimals still strings and nothing re-priced in the browser;
 * * a line the API cannot resolve holds Raise back, because the API would refuse
 *   the whole conversion;
 * * the conversion posts the overrides the buyer last saw previewed, and a 409
 *   duplicate opens the same resolution conversation as a hand-raised order,
 *   whose override the API refuses without `purchase_order.approve`.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type RouteTable } from '@/test/harness'

import type {
  DuplicateCandidateRead,
  RequisitionConversionGroup,
  RequisitionConversionLine,
  RequisitionConversionResult,
  RequisitionConversionTarget,
  RequisitionRow,
} from './procurement-types'
import RequisitionConversionDialog, {
  RequisitionBatchPicker,
} from './requisition-conversion-dialog'

const REQUISITION_ID = '01a00000-0000-7000-8000-0000000000r1'
const OTHER_REQUISITION_ID = '01a00000-0000-7000-8000-0000000000r2'
const LINE_LOCK = '01a00000-0000-7000-8000-00000000ln01'
const LINE_BOX = '01a00000-0000-7000-8000-00000000ln03'
const VENDOR_FAST = '01a00000-0000-7000-8000-00000000vfst'
const VENDOR_PACK = '01a00000-0000-7000-8000-00000000vpck'
const CATEGORY_LOCKS = '01a00000-0000-7000-8000-00000000clck'
const CATEGORY_PACKING = '01a00000-0000-7000-8000-00000000cpck'
const WAREHOUSE_ID = '01a00000-0000-7000-8000-00000000wh01'
const UOM_ID = '01a00000-0000-7000-8000-0000000uom01'
const MANAGER_ID = '01a00000-0000-7000-8000-00000000mgr1'

const PREVIEW_PATH = `/procurement/purchase-requisitions/${REQUISITION_ID}/conversion-preview`
const CONVERT_PATH = `/procurement/purchase-requisitions/${REQUISITION_ID}/convert-to-orders`
const BATCH_PREVIEW_PATH = '/procurement/purchase-requisitions/conversion-preview'

const SINGLE: RequisitionConversionTarget = {
  kind: 'single',
  requisition: { id: REQUISITION_ID, code: 'PR-0001' },
}

function line(overrides: Partial<RequisitionConversionLine> = {}): RequisitionConversionLine {
  return {
    requisition_id: REQUISITION_ID,
    requisition_code: 'PR-0001',
    requisition_line_id: LINE_LOCK,
    line_number: 1,
    item_id: '01a00000-0000-7000-8000-0000000item1',
    item_code: 'BO-LOCKBODY',
    item_name: 'Mortise Lock Body 6572',
    description: null,
    quantity: '100.0000',
    uom_id: UOM_ID,
    required_date: '2026-11-15',
    unit_price: '182.000000',
    price_source: 'LAST_PURCHASE',
    vendor_source: 'ITEM_PREFERRED',
    line_total: '21476.00',
    ...overrides,
  }
}

function group(overrides: Partial<RequisitionConversionGroup> = {}): RequisitionConversionGroup {
  return {
    vendor_id: VENDOR_FAST,
    vendor_code: 'VEN-FAST',
    vendor_name: 'Precision Fasteners',
    item_category_id: CATEGORY_LOCKS,
    item_category_code: 'LOCK-MORT',
    item_category_name: 'Mortise Locks',
    warehouse_id: WAREHOUSE_ID,
    source_requisition_id: REQUISITION_ID,
    assigned_vendor_user_id: MANAGER_ID,
    assigned_vendor_user_name: 'Sandeep Patil',
    assignee_reason: 'CATEGORY_MATCH',
    currency: 'INR',
    expected_delivery_date: '2026-11-15',
    priority: 5,
    subtotal: '18200.00',
    tax_amount: '3276.00',
    total_amount: '21476.00',
    lines: [line()],
    duplicate_candidates: [],
    purchase_order_id: null,
    purchase_order_code: null,
    ...overrides,
  }
}

const BOX_GROUP = group({
  vendor_id: VENDOR_PACK,
  vendor_code: 'VEN-PACK',
  vendor_name: 'Kraft Packaging',
  item_category_id: CATEGORY_PACKING,
  item_category_code: 'PACKING',
  item_category_name: 'Packing material',
  assigned_vendor_user_id: null,
  assigned_vendor_user_name: null,
  assignee_reason: 'AMBIGUOUS',
  lines: [
    line({
      requisition_line_id: LINE_BOX,
      line_number: 3,
      item_code: 'PK-BOX',
      item_name: 'Carton 3-ply',
      quantity: '500.0000',
      unit_price: '14.500000',
      price_source: 'ESTIMATE',
      vendor_source: 'SUGGESTED',
      line_total: '8555.00',
    }),
  ],
})

function result(overrides: Partial<RequisitionConversionResult> = {}): RequisitionConversionResult {
  return {
    requisitions: [],
    groups: [group(), BOX_GROUP],
    unresolved_lines: [],
    skipped_line_count: 0,
    max_orders: 20,
    requires_acknowledgement: false,
    converted: false,
    ...overrides,
  }
}

function candidate(overrides: Partial<DuplicateCandidateRead> = {}): DuplicateCandidateRead {
  return {
    purchase_order_id: '01a00000-0000-7000-8000-000000000po9',
    purchase_order_code: 'PO-0099',
    purchase_order_line_id: '01a00000-0000-7000-8000-00000000pol9',
    status: 'DRAFT',
    vendor_id: VENDOR_FAST,
    item_id: '01a00000-0000-7000-8000-0000000item1',
    warehouse_id: WAREHOUSE_ID,
    ordered_quantity: '100.0000',
    received_quantity: '0.0000',
    open_quantity: '100.0000',
    required_date: '2026-11-15',
    source_type: 'PURCHASE_REQUISITION',
    source_id: REQUISITION_ID,
    confidence: 'EXACT',
    match_reasons: ['Same pending requisition line'],
    recommended_action: 'MERGE_WITH_EXISTING',
    available_actions: ['MERGE_WITH_EXISTING', 'CREATE_NEW_WITH_APPROVAL'],
    ...overrides,
  }
}

const BUYER = [P.PURCHASE_REQUISITION_VIEW, P.PURCHASE_ORDER_CREATE, P.VENDOR_VIEW]

function renderDialog(
  routes: RouteTable,
  {
    permissions = BUYER,
    target = SINGLE,
  }: { permissions?: string[]; target?: RequisitionConversionTarget } = {},
) {
  const onOpenChange = vi.fn()
  const rendered = renderScreen(
    <RequisitionConversionDialog target={target} open onOpenChange={onOpenChange} />,
    { permissions, routes },
  )
  return { ...rendered, onOpenChange }
}

/** Every request to one path, in order. */
function callsTo(fetch: ReturnType<typeof renderScreen>['fetch'], method: string, path: string) {
  return fetch.calls.filter((call) => call.method === method && call.path === path)
}

describe('the proposal', () => {
  it('shows one order per vendor and category, with the sources and the assignee', async () => {
    renderDialog({ [`POST ${PREVIEW_PATH}`]: ok(result()) })

    const lockOrder = await screen.findByRole('region', { name: /Precision Fasteners/ })
    expect(within(lockOrder).getByText('Mortise Locks (LOCK-MORT)')).toBeVisible()
    expect(within(lockOrder).getByText('Sandeep Patil')).toBeVisible()
    expect(within(lockOrder).getByText('Manages this category at the vendor')).toBeVisible()
    expect(within(lockOrder).getByText('Last purchase price')).toBeVisible()
    expect(within(lockOrder).getByText("Item's preferred vendor")).toBeVisible()
    expect(within(lockOrder).getByText('BO-LOCKBODY')).toBeVisible()

    // An ambiguous match is unassigned on purpose, and says who decides.
    const boxOrder = screen.getByRole('region', { name: /Kraft Packaging/ })
    expect(within(boxOrder).getByText('Unassigned')).toBeVisible()
    expect(within(boxOrder).getByText(/choose one when sending/)).toBeVisible()
    expect(within(boxOrder).getByText('Requisition estimate')).toBeVisible()

    expect(screen.getByRole('button', { name: 'Raise 2 purchase orders' })).toBeEnabled()
  })

  it('asks for the preview with no overrides and the duplicate flag unset', async () => {
    const { fetch } = renderDialog({ [`POST ${PREVIEW_PATH}`]: ok(result()) })

    await screen.findByRole('region', { name: /Precision Fasteners/ })
    expect(fetch.requestFor('POST', PREVIEW_PATH)).toEqual({
      lines: [],
      acknowledge_duplicates: false,
    })
  })

  it('names the open orders a proposed order would duplicate', async () => {
    renderDialog({
      [`POST ${PREVIEW_PATH}`]: ok(
        result({
          groups: [group({ duplicate_candidates: [candidate()] })],
          requires_acknowledgement: true,
        }),
      ),
    })

    const lockOrder = await screen.findByRole('region', { name: /Precision Fasteners/ })
    expect(within(lockOrder).getByText('May duplicate an open order')).toBeVisible()
    expect(within(lockOrder).getByRole('link', { name: /PO-0099/ })).toHaveAttribute(
      'href',
      '/procurement/purchase-orders/01a00000-0000-7000-8000-000000000po9',
    )
    expect(within(lockOrder).getByText('Same demand')).toBeVisible()
    expect(screen.getByText('Some of these orders may already be on order')).toBeVisible()
  })

  it('holds Raise back while a line cannot be resolved, and says why', async () => {
    // The API refuses the whole conversion over one unresolved line, so the
    // button that would earn that refusal is not pressable.
    renderDialog({
      [`POST ${PREVIEW_PATH}`]: ok(
        result({
          groups: [group()],
          unresolved_lines: [
            {
              requisition_id: REQUISITION_ID,
              requisition_code: 'PR-0001',
              requisition_line_id: LINE_BOX,
              line_number: 3,
              item_id: '01a00000-0000-7000-8000-0000000item3',
              item_code: 'PK-BOX',
              vendor_id: null,
              reasons: ['No vendor: none was named.'],
            },
          ],
        }),
      ),
    })

    await screen.findByText('1 line cannot be converted yet')
    expect(screen.getByText('No vendor: none was named.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Raise 1 purchase order' })).toBeDisabled()
  })

  it('refuses to raise more orders than one conversion may', async () => {
    renderDialog({
      [`POST ${PREVIEW_PATH}`]: ok(result({ max_orders: 1 })),
    })

    await screen.findByText('Too many orders for one conversion')
    expect(screen.getByRole('button', { name: 'Raise 2 purchase orders' })).toBeDisabled()
  })

  it('reports a preview the API refused instead of an empty proposal', async () => {
    renderDialog({
      [`POST ${PREVIEW_PATH}`]: fail(
        422,
        'BUSINESS_RULE_VIOLATION',
        'Only an approved requisition can be converted into purchase orders: PR-0001 is CLOSED.',
      ),
    })

    await screen.findByText('The proposal could not be worked out')
    expect(screen.getByText(/PR-0001 is CLOSED/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Raise purchase orders' })).toBeDisabled()
  })
})

describe('every correction is a new preview', () => {
  it('re-previews with the typed price as a decimal string', async () => {
    const { user, fetch } = renderDialog({ [`POST ${PREVIEW_PATH}`]: ok(result()) })

    const price = await screen.findByRole('textbox', { name: 'Unit price for PR-0001 line 1' })
    await user.type(price, '175.250000')

    // Once the typing settles, the proposal is asked for again with the price.
    await waitFor(() =>
      expect(callsTo(fetch, 'POST', PREVIEW_PATH).at(-1)!.body).toEqual({
        lines: [
          {
            requisition_line_id: LINE_LOCK,
            vendor_id: null,
            quantity: null,
            // Six places survive: through a float this would be 175.25.
            unit_price: '175.250000',
            exclude: false,
          },
        ],
        acknowledge_duplicates: false,
      }),
    )
  })

  it('does not send a quantity it cannot parse, and holds Raise back', async () => {
    const { user, fetch } = renderDialog({ [`POST ${PREVIEW_PATH}`]: ok(result()) })

    const quantity = await screen.findByRole('textbox', { name: 'Quantity for PR-0001 line 1' })
    await user.type(quantity, '12.34567')

    expect(
      await screen.findByText('A quantity above zero, with up to 4 decimal places'),
    ).toBeVisible()
    expect(screen.getByRole('button', { name: 'Raise 2 purchase orders' })).toBeDisabled()
    // Five places would be a 422: what the proposal is asked for is the last
    // valid state - nothing overridden - never the unparseable one.
    await waitFor(() =>
      expect(callsTo(fetch, 'POST', PREVIEW_PATH).at(-1)!.body).toEqual({
        lines: [],
        acknowledge_duplicates: false,
      }),
    )
    expect(
      callsTo(fetch, 'POST', PREVIEW_PATH).some((call) =>
        JSON.stringify(call.body).includes('12.34567'),
      ),
    ).toBe(false)
  })

  it('leaves a line out, says so, and can bring it back', async () => {
    let previews = 0
    const { user, fetch } = renderDialog({
      [`POST ${PREVIEW_PATH}`]: ({ body }) => {
        previews += 1
        const lines = (body as { lines: { exclude: boolean }[] }).lines
        // Excluded, the box line drops out of the proposal altogether.
        return ok(
          lines.some((entry) => entry.exclude)
            ? result({ groups: [group()], skipped_line_count: 1 })
            : result(),
        )
      },
    })

    await screen.findByRole('region', { name: /Kraft Packaging/ })
    await user.click(screen.getByRole('button', { name: 'Leave out PR-0001 line 3' }))

    await screen.findByText('Left out of this conversion')
    expect(screen.getByText(/PR-0001 line 3 - PK-BOX Carton 3-ply/)).toBeVisible()
    await waitFor(() =>
      expect(screen.queryByRole('region', { name: /Kraft Packaging/ })).not.toBeInTheDocument(),
    )
    expect(callsTo(fetch, 'POST', PREVIEW_PATH).at(-1)!.body).toEqual({
      lines: [
        {
          requisition_line_id: LINE_BOX,
          vendor_id: null,
          quantity: null,
          unit_price: null,
          exclude: true,
        },
      ],
      acknowledge_duplicates: false,
    })

    await user.click(screen.getByRole('button', { name: 'Include again' }))
    await screen.findByRole('region', { name: /Kraft Packaging/ })
    expect(previews).toBeGreaterThanOrEqual(3)
    expect(screen.queryByText('Left out of this conversion')).not.toBeInTheDocument()
  })
})

describe('raising the orders', () => {
  it('posts the overrides that were previewed, and closes with the codes', async () => {
    const { user, fetch, onOpenChange } = renderDialog({
      [`POST ${PREVIEW_PATH}`]: ok(result()),
      [`POST ${CONVERT_PATH}`]: ok(
        result({
          converted: true,
          groups: [
            group({ purchase_order_id: 'po-1', purchase_order_code: 'PO/2026-27/00041' }),
            { ...BOX_GROUP, purchase_order_id: 'po-2', purchase_order_code: 'PO/2026-27/00042' },
          ],
        }),
      ),
    })

    await user.click(await screen.findByRole('button', { name: 'Raise 2 purchase orders' }))

    await waitFor(() => fetch.requestFor('POST', CONVERT_PATH))
    expect(fetch.requestFor('POST', CONVERT_PATH)).toEqual({
      lines: [],
      acknowledge_duplicates: false,
    })
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
  })

  it('sends the batch form with every requisition named', async () => {
    const batch: RequisitionConversionTarget = {
      kind: 'batch',
      requisitions: [
        { id: REQUISITION_ID, code: 'PR-0001' },
        { id: OTHER_REQUISITION_ID, code: 'PR-0002' },
      ],
    }
    const { fetch } = renderDialog(
      { [`POST ${BATCH_PREVIEW_PATH}`]: ok(result()) },
      { target: batch },
    )

    await screen.findByRole('heading', { name: 'Raise purchase orders from 2 requisitions' })
    await waitFor(() => fetch.requestFor('POST', BATCH_PREVIEW_PATH))
    expect(fetch.requestFor('POST', BATCH_PREVIEW_PATH)).toEqual({
      lines: [],
      acknowledge_duplicates: false,
      requisition_ids: [REQUISITION_ID, OTHER_REQUISITION_ID],
    })
  })
})

describe('the duplicate refusal', () => {
  const refusal = fail(
    409,
    'DUPLICATE_DOCUMENT',
    'This order duplicates 1 open purchase order line(s) - PO-0099 already covers the same item.',
    {
      candidate_count: 1,
      candidates: [
        {
          purchase_order_id: '01a00000-0000-7000-8000-000000000po9',
          purchase_order_code: 'PO-0099',
          purchase_order_line_id: '01a00000-0000-7000-8000-00000000pol9',
          item_id: '01a00000-0000-7000-8000-0000000item1',
          status: 'DRAFT',
          open_quantity: '100.0000',
          required_date: '2026-11-15',
          confidence: 'EXACT',
          match_reasons: ['Same pending requisition line'],
          recommended_action: 'MERGE_WITH_EXISTING',
          available_actions: ['MERGE_WITH_EXISTING', 'CREATE_NEW_WITH_APPROVAL'],
        },
      ],
    },
  )

  it('shows which order already covers it, and no override without approval rights', async () => {
    const { user } = renderDialog({
      [`POST ${PREVIEW_PATH}`]: ok(result()),
      [`POST ${CONVERT_PATH}`]: refusal,
    })

    await user.click(await screen.findByRole('button', { name: 'Raise 2 purchase orders' }))

    const dialog = await screen.findByRole('dialog', {
      name: 'This requirement may already be on order',
    })
    expect(within(dialog).getByText(/No purchase order was raised/)).toBeVisible()
    expect(within(dialog).getByRole('link', { name: /PO-0099/ })).toBeVisible()
    expect(within(dialog).getByText('You cannot override this yourself')).toBeVisible()
    expect(within(dialog).queryByRole('button', { name: 'Raise them anyway' })).toBeNull()
  })

  it('resubmits with the acknowledgement and the reason once an approver overrides', async () => {
    let attempts = 0
    const { user, fetch, onOpenChange } = renderDialog(
      {
        [`POST ${PREVIEW_PATH}`]: ok(result()),
        [`POST ${CONVERT_PATH}`]: () => {
          attempts += 1
          return attempts === 1
            ? refusal
            : ok(result({ converted: true, groups: [group({ purchase_order_code: 'PO-0100' })] }))
        },
      },
      { permissions: [...BUYER, P.PURCHASE_ORDER_APPROVE] },
    )

    await user.click(await screen.findByRole('button', { name: 'Raise 2 purchase orders' }))
    const dialog = await screen.findByRole('dialog', {
      name: 'This requirement may already be on order',
    })
    await user.type(
      within(dialog).getByRole('textbox', { name: /why is this order needed as well/i }),
      '  Second press line starts in November  ',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Raise them anyway' }))

    await waitFor(() => expect(callsTo(fetch, 'POST', CONVERT_PATH)).toHaveLength(2))
    expect(callsTo(fetch, 'POST', CONVERT_PATH)[1]!.body).toEqual({
      lines: [],
      acknowledge_duplicates: true,
      duplicate_acknowledgement_reason: 'Second press line starts in November',
    })
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
  })
})

describe('the batch picker', () => {
  function convertible(overrides: Partial<RequisitionRow> = {}): RequisitionRow {
    return {
      id: REQUISITION_ID,
      code: 'PR-0001',
      status: 'APPROVED',
      requisition_date: '2026-10-01',
      required_date: '2026-11-15',
      priority: 5,
      purpose: 'Bought-out lock bodies',
      source_type: 'MRP_SUGGESTION',
      source_id: null,
      department_id: null,
      warehouse_id: WAREHOUSE_ID,
      requested_by_id: null,
      approved_by_id: null,
      approved_at: '2026-10-02T09:00:00Z',
      created_at: '2026-10-01T09:00:00Z',
      open_line_count: 1,
      ...overrides,
    }
  }

  it('lists only what is still to order, and hands back the ticked requisitions', async () => {
    let convertibleOnly: string | null = null
    const onContinue = vi.fn()
    const { user } = renderScreen(
      <RequisitionBatchPicker open onOpenChange={() => undefined} onContinue={onContinue} />,
      {
        permissions: BUYER,
        routes: {
          'GET /procurement/purchase-requisitions': ({ url }) => {
            convertibleOnly = url.searchParams.get('convertible_only')
            return page([
              convertible(),
              convertible({ id: OTHER_REQUISITION_ID, code: 'PR-0002', open_line_count: 3 }),
            ])
          },
        },
      },
    )

    await user.click(await screen.findByRole('checkbox', { name: 'Select PR-0002' }))
    await user.click(screen.getByRole('checkbox', { name: 'Select PR-0001' }))
    expect(screen.getByText('2 selected')).toBeVisible()
    expect(screen.getByText('3 lines')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Review purchase orders' }))
    expect(convertibleOnly).toBe('true')
    expect(onContinue).toHaveBeenCalledWith([
      { id: OTHER_REQUISITION_ID, code: 'PR-0002' },
      { id: REQUISITION_ID, code: 'PR-0001' },
    ])
  })

  it('will not continue with nothing ticked', async () => {
    renderScreen(
      <RequisitionBatchPicker open onOpenChange={() => undefined} onContinue={() => undefined} />,
      {
        permissions: BUYER,
        routes: { 'GET /procurement/purchase-requisitions': page([convertible()]) },
      },
    )

    await screen.findByRole('checkbox', { name: 'Select PR-0001' })
    expect(screen.getByRole('button', { name: 'Review purchase orders' })).toBeDisabled()
  })
})
