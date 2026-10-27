import { screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { P } from '@/lib/permissions'
import { messages } from '@/lib/messages'
import { cursorPage, fail, ok, page, renderScreen, type RouteTable } from '@/test/harness'

import PurchaseOrderAmendDialog from './purchase-order-amend-dialog'
import PurchaseOrderAmendmentsPanel from './purchase-order-amendments-panel'
import { PurchaseOrderDuplicateDialog } from './purchase-order-duplicate-dialog'
import PurchaseOrderDetailPage from './purchase-order-detail-page'
import type {
  DuplicateCandidate,
  DuplicateConflict,
  PurchaseOrderDetail,
  PurchaseOrderLine,
} from './procurement-types'
import type {
  DuplicateResolutionDraft,
  PurchaseOrderAmendment,
} from './purchase-order-change-model'

const PO = '019d0000-0000-7000-8000-000000000101'
const LINE = '019d0000-0000-7000-8000-000000000102'
const ITEM = '019d0000-0000-7000-8000-000000000103'
const PR_LINE = '019d0000-0000-7000-8000-000000000104'
const BASE = `/procurement/purchase-orders/${PO}`
const ACTION = '/procurement/purchase-orders/duplicate-resolution'

function line(overrides: Partial<PurchaseOrderLine> = {}): PurchaseOrderLine {
  return {
    id: LINE,
    purchase_order_id: PO,
    line_number: 1,
    item_id: ITEM,
    item_code: 'BRASS-HANDLE',
    item_name: 'Brass handle',
    description: 'Brass handle',
    quantity: '10.0000',
    uom_id: ITEM,
    uom_code: 'NOS',
    unit_price: '125.123456',
    discount_percentage: '0',
    discount: '0',
    tax_rate: '18',
    tax_amount: '225.22',
    line_total: '1476.45',
    required_date: null,
    received_quantity: '0',
    accepted_quantity: '0',
    rejected_quantity: '0',
    returned_quantity: '0',
    requires_qc: false,
    status: 'PENDING',
    requisition_line_id: null,
    quotation_line_id: null,
    notes: null,
    source_allocations: [],
    ...overrides,
  }
}
function order(overrides: Partial<PurchaseOrderDetail> = {}): PurchaseOrderDetail {
  return {
    id: PO,
    code: 'PO-0001',
    vendor_id: ITEM,
    warehouse_id: ITEM,
    po_date: '2026-10-27',
    expected_delivery_date: '2026-11-10',
    status: 'SENT',
    priority: 5,
    currency: 'INR',
    subtotal: '1251.23',
    discount: '0',
    tax_amount: '225.22',
    freight_amount: '0',
    total_amount: '1476.45',
    payment_terms_days: 30,
    approved_at: '2026-10-27T10:00:00Z',
    sent_at: '2026-10-27T11:00:00Z',
    acknowledged_at: null,
    source_type: 'MANUAL',
    source_id: null,
    assigned_vendor_user_id: null,
    item_category_id: null,
    version: 4,
    amendment_number: 0,
    created_at: '2026-10-27T09:00:00Z',
    branch_id: null,
    approved_by_id: null,
    delivery_address: null,
    terms: null,
    notes: null,
    vendor_reference: null,
    rejection_reason: null,
    cancellation_reason: null,
    duplicate_acknowledged: false,
    duplicate_acknowledgement_reason: null,
    has_receipt_history: false,
    courier_name: null,
    tracking_reference: null,
    shipped_at: null,
    lines: [line()],
    ...overrides,
  }
}
function candidate(overrides: Partial<DuplicateCandidate> = {}): DuplicateCandidate {
  return {
    purchase_order_id: PO,
    purchase_order_code: 'PO-0001',
    purchase_order_line_id: LINE,
    purchase_order_version: 4,
    item_id: ITEM,
    status: 'DRAFT',
    has_receipt_history: false,
    open_quantity: '10',
    required_date: '2026-11-10',
    confidence: 'EXACT',
    match_reasons: ['Same pending requisition line'],
    recommended_action: 'MERGE_WITH_EXISTING',
    available_actions: [
      'MERGE_WITH_EXISTING',
      'INCREASE_EXISTING_QUANTITY',
      'CREATE_NEW_WITH_APPROVAL',
    ],
    ...overrides,
  }
}
const MANUAL: DuplicateResolutionDraft = {
  kind: 'MANUAL',
  purchase_order: {
    vendor_id: ITEM,
    warehouse_id: ITEM,
    priority: 5,
    currency: 'INR',
    freight_amount: '0',
    source_type: 'MANUAL',
    acknowledge_duplicates: false,
    lines: [
      {
        line_number: 1,
        item_id: ITEM,
        quantity: '5.0000',
        unit_price: '125.123456',
        requires_qc: false,
      },
    ],
  },
}
function conflict(match = candidate()): DuplicateConflict {
  return {
    message: 'The same pending demand is already on order.',
    candidate_count: 1,
    candidates: [match],
  }
}
function resolutionDialog({
  rights = [P.PURCHASE_ORDER_CREATE, P.PURCHASE_ORDER_UPDATE, P.PURCHASE_REQUISITION_CREATE],
  match = candidate(),
  draft = MANUAL,
  routes = {},
}: {
  rights?: string[]
  match?: DuplicateCandidate
  draft?: DuplicateResolutionDraft
  routes?: RouteTable
} = {}) {
  const onResolved = vi.fn()
  const onRefresh = vi.fn(async () => {})
  return {
    ...renderScreen(
      <PurchaseOrderDuplicateDialog
        conflict={conflict(match)}
        draft={draft}
        onConflict={vi.fn()}
        onOpenChange={vi.fn()}
        onResolved={onResolved}
        onRefresh={onRefresh}
      />,
      { permissions: rights, routes },
    ),
    onResolved,
    onRefresh,
  }
}

describe('real duplicate resolution commands', () => {
  it.each([
    ['Merge into PO-0001', 'MERGE'],
    ['Increase PO-0001 line qty', 'INCREASE'],
    ['Create separate PO (needs approval)', 'CREATE_WITH_APPROVAL'],
    ['Discard this draft', 'CANCEL_REQUEST'],
  ] as const)(
    'sends the refused manual draft and audited reason for %s',
    async (button, action) => {
      const view = resolutionDialog({
        routes: {
          [`POST ${ACTION}`]: ok({
            action,
            purchase_orders: action === 'CANCEL_REQUEST' ? [] : [order()],
            cancelled_requisition_line_ids: [],
            discarded: action === 'CANCEL_REQUEST',
          }),
        },
      })
      await view.user.click(await screen.findByRole('button', { name: button }))
      expect(
        view.fetch.calls.filter((call) => call.method === 'POST' && call.path === ACTION),
      ).toHaveLength(0)
      expect(screen.getByText(messages.errors.reasonTooShort)).toBeVisible()
      await view.user.type(
        screen.getByRole('textbox', { name: 'Reason for this action' }),
        '  Additional customer demand  ',
      )
      await view.user.click(screen.getByRole('button', { name: button }))
      await waitFor(() => expect(view.onResolved).toHaveBeenCalledTimes(1))
      expect(view.fetch.requestFor('POST', ACTION)).toEqual({
        action,
        draft: MANUAL,
        reason: 'Additional customer demand',
        ...(['MERGE', 'INCREASE'].includes(action)
          ? { candidate_purchase_order_id: PO, expected_candidate_version: 4 }
          : {}),
        ...(action === 'INCREASE' ? { candidate_line_id: LINE } : {}),
      })
    },
  )

  it('requires only create permission for requesting approval, while hiding edit and cancel commands', async () => {
    resolutionDialog({ rights: [P.PURCHASE_ORDER_CREATE] })
    expect(
      await screen.findByRole('button', { name: 'Create separate PO (needs approval)' }),
    ).toBeVisible()
    expect(
      screen.queryByRole('button', { name: /Merge into|Increase .*line qty|Discard this draft/ }),
    ).toBeNull()
  })

  it('keeps every incoming manual item by withholding INCREASE for a mixed draft', async () => {
    if (MANUAL.kind !== 'MANUAL') throw new Error('Manual fixture expected')
    const draft: DuplicateResolutionDraft = {
      kind: 'MANUAL',
      purchase_order: {
        ...MANUAL.purchase_order,
        lines: [
          ...MANUAL.purchase_order.lines,
          { ...MANUAL.purchase_order.lines[0]!, line_number: 2, item_id: 'other-item' },
        ],
      },
    }
    resolutionDialog({ draft })
    expect(await screen.findByRole('button', { name: 'Merge into PO-0001' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Increase PO-0001 line qty' })).toBeNull()
  })

  it('labels a sent candidate as an amendment, and refuses all changes after any receipt history', async () => {
    resolutionDialog({ match: candidate({ status: 'SENT', has_receipt_history: true }) })
    expect(await screen.findByText(/Receipt history prevents changes/)).toBeVisible()
    expect(screen.queryByRole('button', { name: /Amend PO|Merge into|Increase PO/ })).toBeNull()
  })

  it('says the vendor must re-accept when a sent candidate is amended', async () => {
    resolutionDialog({ match: candidate({ status: 'SENT' }) })
    expect(
      await screen.findByRole('button', { name: 'Amend PO-0001 (vendor re-accepts)' }),
    ).toBeVisible()
  })

  it('shows a version conflict and lets the operator refresh without losing their reason', async () => {
    function Scenario() {
      const [match, setMatch] = useState(candidate())
      return (
        <PurchaseOrderDuplicateDialog
          conflict={conflict(match)}
          draft={MANUAL}
          onConflict={vi.fn()}
          onResolved={vi.fn()}
          onOpenChange={vi.fn()}
          onRefresh={async () => setMatch(candidate({ purchase_order_version: 5 }))}
        />
      )
    }
    let attempts = 0
    const view = renderScreen(<Scenario />, {
      permissions: [P.PURCHASE_ORDER_UPDATE],
      routes: {
        [`POST ${ACTION}`]: () =>
          ++attempts === 1
            ? fail(
                409,
                'CONCURRENT_MODIFICATION',
                'This purchase order changed. Refresh before continuing.',
              )
            : ok({
                action: 'MERGE',
                purchase_orders: [order()],
                cancelled_requisition_line_ids: [],
                discarded: false,
              }),
      },
    })
    await view.user.type(
      await screen.findByRole('textbox', { name: 'Reason for this action' }),
      'Additional customer demand',
    )
    await view.user.click(screen.getByRole('button', { name: 'Merge into PO-0001' }))
    expect(
      await screen.findByText('This purchase order changed. Refresh before continuing.'),
    ).toBeVisible()
    await view.user.click(screen.getByRole('button', { name: 'Refresh comparison' }))
    await waitFor(() =>
      expect(
        screen.queryByText('This purchase order changed. Refresh before continuing.'),
      ).toBeNull(),
    )
    expect(screen.getByRole('textbox', { name: 'Reason for this action' })).toHaveValue(
      'Additional customer demand',
    )
    await view.user.click(screen.getByRole('button', { name: 'Merge into PO-0001' }))
    await waitFor(() =>
      expect(view.fetch.calls.filter((call) => call.path === ACTION)).toHaveLength(2),
    )
    expect(view.fetch.calls.filter((call) => call.path === ACTION)[1]?.body).toMatchObject({
      expected_candidate_version: 5,
    })
  })
})

describe('purchase order amendment editor', () => {
  it('keeps the opening revision and version when background data changes during editing', async () => {
    const onClose = vi.fn()
    const opening = order()
    const view = renderScreen(<PurchaseOrderAmendDialog order={opening} onClose={onClose} />, {
      permissions: [P.PURCHASE_ORDER_UPDATE],
      routes: {
        [`POST ${BASE}/amend`]: ({ body }) =>
          (body as { expected_version: number }).expected_version === 4
            ? fail(
                409,
                'CONCURRENT_MODIFICATION',
                'A newer revision was approved. Refresh the order before amending it.',
              )
            : ok(order({ version: 6, amendment_number: 2, status: 'PENDING_APPROVAL' })),
      },
    })
    await view.user.type(
      await screen.findByRole('textbox', { name: 'Reason for amendment' }),
      'Adjust the original purchase quantity',
    )
    await view.user.clear(screen.getByRole('textbox', { name: 'Quantity for line 1' }))
    await view.user.type(screen.getByRole('textbox', { name: 'Quantity for line 1' }), '12')

    view.rerender(
      <PurchaseOrderAmendDialog
        order={order({
          version: 5,
          amendment_number: 1,
          lines: [line({ quantity: '30', unit_price: '200' })],
        })}
        onClose={onClose}
      />,
    )
    expect(screen.getByText(/Save revision 1 for fresh approval/)).toBeVisible()
    expect(screen.getByRole('textbox', { name: 'Quantity for line 1' })).toHaveValue('12')
    expect(screen.getByRole('textbox', { name: 'Unit price for line 1' })).toHaveValue('125.123456')
    await view.user.click(screen.getByRole('button', { name: 'Save amendment' }))
    expect(
      await screen.findByText(
        'A newer revision was approved. Refresh the order before amending it.',
      ),
    ).toBeVisible()
    expect(view.fetch.requestFor('POST', `${BASE}/amend`)).toMatchObject({
      expected_version: 4,
      lines: [{ line_id: LINE, quantity: '12', unit_price: '125.123456' }],
    })
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox', { name: 'Reason for amendment' })).toHaveValue(
      'Adjust the original purchase quantity',
    )
    await view.user.click(screen.getByRole('button', { name: 'Close and refresh order' }))
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('retains line IDs and the document version while applying exact quantities and prices', async () => {
    const onClose = vi.fn()
    const view = renderScreen(<PurchaseOrderAmendDialog order={order()} onClose={onClose} />, {
      permissions: [P.PURCHASE_ORDER_UPDATE],
      routes: {
        [`POST ${BASE}/amend`]: ok(order({ status: 'PENDING_APPROVAL', amendment_number: 1 })),
      },
    })
    await view.user.type(
      await screen.findByRole('textbox', { name: 'Reason for amendment' }),
      'Increase the quantity for a new order',
    )
    const quantity = screen.getByRole('textbox', { name: 'Quantity for line 1' })
    await view.user.clear(quantity)
    await view.user.type(quantity, '12.1250')
    await view.user.click(screen.getByRole('button', { name: 'Save amendment' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
    expect(view.fetch.requestFor('POST', `${BASE}/amend`)).toEqual({
      expected_version: 4,
      reason: 'Increase the quantity for a new order',
      lines: [{ line_id: LINE, line_number: 1, quantity: '12.1250', unit_price: '125.123456' }],
    })
  })

  it('requires an explicit source decision for a reduction and releases only the chosen quantity', async () => {
    const current = order({
      lines: [
        line({
          source_allocations: [
            {
              requisition_line_id: PR_LINE,
              quantity: '10.0000',
              requisition_code: 'PR-0007',
              requisition_line_number: 2,
              is_active: true,
            },
          ],
        }),
      ],
    })
    const view = renderScreen(<PurchaseOrderAmendDialog order={current} onClose={vi.fn()} />, {
      permissions: [P.PURCHASE_ORDER_UPDATE],
      routes: {
        [`POST ${BASE}/amend`]: ok(order({ status: 'PENDING_APPROVAL', amendment_number: 1 })),
      },
    })
    await view.user.type(
      await screen.findByRole('textbox', { name: 'Reason for amendment' }),
      'Reduce the remaining production demand',
    )
    await view.user.clear(screen.getByRole('textbox', { name: 'Quantity for line 1' }))
    await view.user.type(screen.getByRole('textbox', { name: 'Quantity for line 1' }), '6')
    await view.user.click(screen.getByRole('button', { name: 'Save amendment' }))
    expect(await screen.findByText(/Choose the requisition quantities to retain/)).toBeVisible()
    expect(
      view.fetch.calls.some((call) => call.method === 'POST' && call.path.endsWith('/amend')),
    ).toBe(false)
    await view.user.clear(screen.getByRole('textbox', { name: 'PR-0007 line 2' }))
    await view.user.type(screen.getByRole('textbox', { name: 'PR-0007 line 2' }), '6')
    await view.user.click(screen.getByRole('button', { name: 'Save amendment' }))
    await waitFor(() =>
      expect(view.fetch.requestFor('POST', `${BASE}/amend`)).toMatchObject({
        lines: [
          {
            line_id: LINE,
            quantity: '6',
            source_allocations: [{ requisition_line_id: PR_LINE, quantity: '6' }],
          },
        ],
      }),
    )
  })

  it('renders field errors and a safe refresh path for a stale revision without discarding edits', async () => {
    const onClose = vi.fn()
    const view = renderScreen(<PurchaseOrderAmendDialog order={order()} onClose={onClose} />, {
      permissions: [P.PURCHASE_ORDER_UPDATE],
      routes: {
        [`POST ${BASE}/amend`]: fail(
          409,
          'CONCURRENT_MODIFICATION',
          'The purchase order changed after this editor opened.',
        ),
      },
    })
    await view.user.type(
      await screen.findByRole('textbox', { name: 'Reason for amendment' }),
      'Correct the agreed order quantity',
    )
    await view.user.click(screen.getByRole('button', { name: 'Save amendment' }))
    expect(
      await screen.findByText('The purchase order changed after this editor opened.'),
    ).toBeVisible()
    expect(screen.getByRole('textbox', { name: 'Reason for amendment' })).toHaveValue(
      'Correct the agreed order quantity',
    )
    await view.user.click(screen.getByRole('button', { name: 'Close and refresh order' }))
    expect(onClose).toHaveBeenCalledOnce()
  })
})

function detailRoutes(current: PurchaseOrderDetail, extra: RouteTable = {}): RouteTable {
  return {
    [`GET ${BASE}/tracking`]: ok({
      purchase_order_id: PO,
      code: current.code,
      status: current.status,
      vendor_name: 'Brass Works',
      receipts: [],
      ordered_quantity: '10',
      received_quantity: '0',
      pending_quantity: '10',
      accepted_quantity: '0',
      rejected_quantity: '0',
    }),
    [`GET ${BASE}/shipments`]: page([]),
    [`GET ${BASE}/documents`]: page([]),
    [`GET ${BASE}`]: ok(current),
    ...extra,
  }
}

describe('duplicate confirmation and revision visibility', () => {
  it('requires the duplicate checkbox and a meaningful reason at approval', async () => {
    const current = order({
      status: 'PENDING_APPROVAL',
      duplicate_acknowledged: true,
      duplicate_acknowledgement_reason: 'Separate demand for a new press line',
    })
    const view = renderScreen(<PurchaseOrderDetailPage />, {
      route: BASE,
      path: '/procurement/purchase-orders/:poId',
      permissions: [P.PURCHASE_ORDER_VIEW, P.PURCHASE_ORDER_APPROVE],
      routes: detailRoutes(current, {
        [`POST ${BASE}/approve`]: ok(order({ status: 'APPROVED' })),
      }),
    })
    await view.user.click(await screen.findByRole('button', { name: 'Approve' }))
    const dialog = screen.getByRole('dialog', { name: /Approve PO-0001/ })
    expect(within(dialog).getByText('Separate demand for a new press line')).toBeVisible()
    await view.user.click(within(dialog).getByRole('button', { name: 'Approve order' }))
    expect(within(dialog).getByText(/Confirm that this duplicate demand is required/)).toBeVisible()
    await view.user.click(
      within(dialog).getByRole('checkbox', {
        name: 'I confirm that the duplicate demand is required',
      }),
    )
    await view.user.click(within(dialog).getByRole('button', { name: 'Approve order' }))
    expect(within(dialog).getByText(messages.errors.reasonTooShort)).toBeVisible()
    await view.user.type(
      within(dialog).getByRole('textbox', { name: 'Reason' }),
      'Verified additional demand with planning',
    )
    await view.user.click(within(dialog).getByRole('button', { name: 'Approve order' }))
    await waitFor(() =>
      expect(view.fetch.requestFor('POST', `${BASE}/approve`)).toEqual({
        reason: 'Verified additional demand with planning',
        confirm_duplicate: true,
      }),
    )
  })

  it('hides amendment once receipt history exists even when received quantity is zero', async () => {
    renderScreen(<PurchaseOrderDetailPage />, {
      route: BASE,
      path: '/procurement/purchase-orders/:poId',
      permissions: [P.PURCHASE_ORDER_VIEW, P.PURCHASE_ORDER_UPDATE],
      routes: detailRoutes(order({ has_receipt_history: true })),
    })
    await screen.findByRole('heading', { name: 'PO-0001' })
    expect(screen.queryByRole('button', { name: 'Amend' })).toBeNull()
  })

  it('shows the amendment command only for a supported state and update permission', async () => {
    renderScreen(<PurchaseOrderDetailPage />, {
      route: BASE,
      path: '/procurement/purchase-orders/:poId',
      permissions: [P.PURCHASE_ORDER_VIEW, P.PURCHASE_ORDER_UPDATE],
      routes: detailRoutes(order()),
    })
    expect(await screen.findByRole('button', { name: 'Amend' })).toBeVisible()
  })

  it('loads a keyset revision history through PO view, including before and after terms', async () => {
    const before = order()
    const revision: PurchaseOrderAmendment = {
      id: 'revision-1',
      amendment_number: 1,
      amended_at: '2026-10-27T12:00:00Z',
      amended_by_id: ITEM,
      amended_by_name: 'Purchase Buyer',
      reason: 'Additional demand for customer order',
      from_status: 'SENT',
      old_values: {
        status: 'SENT',
        expected_delivery_date: before.expected_delivery_date,
        freight_amount: '0',
        terms: null,
        delivery_address: null,
        amendment_number: 0,
        lines: [line()],
      },
      new_values: {
        status: 'PENDING_APPROVAL',
        expected_delivery_date: before.expected_delivery_date,
        freight_amount: '0',
        terms: null,
        delivery_address: null,
        amendment_number: 1,
        lines: [line({ quantity: '12' })],
      },
    }
    const view = renderScreen(<PurchaseOrderAmendmentsPanel orderId={PO} currency="INR" />, {
      permissions: [P.PURCHASE_ORDER_VIEW],
      routes: { [`GET ${BASE}/amendments`]: cursorPage([revision]) },
    })
    expect(await screen.findByText('Additional demand for customer order')).toBeVisible()
    await view.user.click(screen.getByRole('button', { name: 'Show revision 1 changes' }))
    expect(
      within(screen.getByRole('region', { name: 'Before revision' })).getByText('10 NOS'),
    ).toBeVisible()
    expect(
      within(screen.getByRole('region', { name: 'After revision' })).getByText('12 NOS'),
    ).toBeVisible()
    expect(view.fetch.calls.some((call) => call.path.startsWith('/audit/'))).toBe(false)
  })
})
