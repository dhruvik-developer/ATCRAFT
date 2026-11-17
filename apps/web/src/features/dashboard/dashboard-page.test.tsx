/**
 * Management dashboard.
 *
 * Every panel here is gated on the right the API enforces on the request
 * behind it, and the harness throws on an unrouted request - so a panel that
 * asks without the right fails on the request itself, not on an assertion.
 * The two defects pinned below both passed a clean build: the constants they
 * mis-gated on exist, and both spellings are real.
 */

import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { cursorPage, fail, ok, page, renderScreen, type RouteTable } from '@/test/harness'
import { P } from '@/lib/permissions'

import DashboardPage from './dashboard-page'

/** `GET /reports/dashboard` - the `DashboardSummary` payload. */
const SUMMARY = {
  open_sales_orders: 4,
  overdue_sales_orders: 1,
  pending_purchase_orders: 2,
  pending_purchase_value: '125000.00',
  production_in_progress: 3,
  qc_pending: 0,
  dispatches_in_transit: 1,
  overdue_deliveries: 0,
  stock_value: '9876543.21',
  stock_refreshed_at: '2026-09-04T06:00:00Z',
  generated_at: '2026-09-04T06:15:00Z',
}

function renderDashboard(permissions: string[], routes: RouteTable = {}) {
  return renderScreen(<DashboardPage />, {
    permissions,
    routes: {
      'GET /reports/dashboard/trends': ok({
        date_from: '2026-09-03',
        date_to: '2026-09-04',
        series: [],
        generated_at: '2026-09-04T06:15:00Z',
      }),
      ...routes,
    },
  })
}

it('reads recent activity using the audit endpoint cursor contract', async () => {
  renderDashboard([P.AUDIT_LOG_VIEW], {
    'GET /audit/logs': cursorPage([
      {
        id: 'audit-1',
        action: 'CREATE',
        entity_type: 'item',
        entity_id: 'item-1',
        entity_code: 'ITEM-1',
        summary: 'Catalogue item created',
        actor_label: 'Operator',
        created_at: '2026-09-04T06:00:00Z',
      },
    ]),
  })
  expect(await screen.findByText(/Catalogue item created/)).toBeVisible()
  expect(screen.queryByText('Activity unavailable')).not.toBeInTheDocument()
})

describe('the summary is requested only with dashboard.view', () => {
  it('shows the tiles the account may open once the summary arrives', async () => {
    const { fetch } = renderDashboard([P.DASHBOARD_VIEW, P.SALES_ORDER_VIEW], {
      'GET /reports/dashboard': ok(SUMMARY),
    })

    expect(await screen.findByText('Open sales orders')).toBeVisible()
    await waitFor(() => fetch.requestFor('GET', '/reports/dashboard'))
    // Tiles follow the sidebar's view rights: no dispatch right, no dispatch tile.
    expect(screen.queryByText('Dispatches in transit')).toBeNull()
  })

  it('shows an initial summary failure without claiming the account lacks panel permissions', async () => {
    renderDashboard([P.DASHBOARD_VIEW, P.SALES_ORDER_VIEW], {
      'GET /reports/dashboard': fail(
        503,
        'SERVICE_UNAVAILABLE',
        'Dashboard is temporarily unavailable.',
      ),
    })

    expect(await screen.findByText('Could not load the dashboard')).toBeVisible()
    expect(screen.getByText(/^Dashboard is temporarily unavailable\./)).toBeVisible()
    expect(screen.queryByText('Nothing to show on your dashboard')).not.toBeInTheDocument()
    expect(
      screen.queryByText(
        'Your account does not hold the permissions these panels need. An administrator can grant them.',
      ),
    ).not.toBeInTheDocument()
  })

  it('never asks for the summary without the right, and offers no tile that could only show a dash', async () => {
    // Sign-in lands on `/` by default, so an account without `dashboard.view`
    // used to collect a 403 and a "Not permitted" toast on every visit.
    const { fetch } = renderDashboard([P.SALES_ORDER_VIEW])

    expect(await screen.findByText('Nothing to show on your dashboard')).toBeVisible()
    expect(screen.queryByText('Open sales orders')).toBeNull()
    expect(fetch.calls.some((call) => call.path === '/reports/dashboard')).toBe(false)
  })
})

describe('legacy summary figures respect their owning permissions', () => {
  const legacyRights = [
    P.DASHBOARD_VIEW,
    P.SALES_ORDER_VIEW,
    P.INVENTORY_VALUATION_VIEW,
    P.PURCHASE_ORDER_VIEW,
    P.PRODUCTION_ORDER_VIEW,
    P.QC_VIEW,
    P.DISPATCH_VIEW,
  ]
  const legacyLabels = [
    'Open sales orders',
    'Overdue sales orders',
    'Stock value',
    'Open purchase orders',
    'Production in progress',
    'QC pending',
    'Dispatches in transit',
    'Overdue deliveries',
  ]

  it('withholds stock value from an inventory reader without the valuation right', async () => {
    renderDashboard([P.DASHBOARD_VIEW, P.INVENTORY_VIEW], {
      // An older cached response may still contain the value. The affordance
      // must follow the current valuation permission even for that response.
      'GET /reports/dashboard': ok(SUMMARY),
    })

    await screen.findByText(/^Summary tables last refreshed/)
    expect(screen.queryByText('Stock value')).not.toBeInTheDocument()
    expect(screen.queryByText(/On-hand at valuation/)).not.toBeInTheDocument()
  })

  it('shows a permitted valuation without linking to an inventory screen the account cannot open', async () => {
    renderDashboard([P.DASHBOARD_VIEW, P.INVENTORY_VALUATION_VIEW], {
      'GET /reports/dashboard': ok(SUMMARY),
    })

    await screen.findByText(/^Summary tables last refreshed/)
    expect(screen.getByText('Stock value')).toBeVisible()
    expect(screen.getByText(/On-hand at valuation/)).toBeVisible()
    expect(screen.queryByRole('link', { name: /Stock value/ })).not.toBeInTheDocument()
  })

  it('links valuation to inventory when the account also holds the balance view right', async () => {
    renderDashboard([P.DASHBOARD_VIEW, P.INVENTORY_VALUATION_VIEW, P.INVENTORY_VIEW], {
      'GET /reports/dashboard': ok(SUMMARY),
    })

    await screen.findByText(/^Summary tables last refreshed/)
    expect(screen.getByRole('link', { name: /Stock value/ })).toHaveAttribute('href', '/inventory')
  })

  it('drops every withheld legacy tile after loading instead of presenting an empty figure', async () => {
    renderDashboard(legacyRights, {
      'GET /reports/dashboard': ok({
        ...SUMMARY,
        open_sales_orders: null,
        overdue_sales_orders: null,
        stock_value: null,
        stock_refreshed_at: null,
        pending_purchase_orders: null,
        pending_purchase_value: null,
        production_in_progress: null,
        qc_pending: null,
        dispatches_in_transit: null,
        overdue_deliveries: null,
      }),
    })

    await screen.findByText(/^Summary tables last refreshed/)
    for (const label of legacyLabels) expect(screen.queryByText(label)).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Key figures' })).not.toBeInTheDocument()
  })

  it('keeps zero counts and valuation as business facts, and leaves a withheld commitment unlabelled', async () => {
    renderDashboard(legacyRights, {
      'GET /reports/dashboard': ok({
        ...SUMMARY,
        open_sales_orders: 0,
        overdue_sales_orders: 0,
        stock_value: '0.00',
        pending_purchase_orders: 0,
        pending_purchase_value: null,
        production_in_progress: 0,
        qc_pending: 0,
        dispatches_in_transit: 0,
        overdue_deliveries: 0,
      }),
    })

    await screen.findByText(/^Summary tables last refreshed/)
    for (const label of legacyLabels) expect(screen.getByText(label)).toBeVisible()
    expect(screen.getAllByText('0')).toHaveLength(7)
    expect(screen.getByText('₹0')).toBeVisible()
    expect(screen.queryByText(/committed$/)).not.toBeInTheDocument()
    expect(screen.getByText('Approved and not yet fully received')).toBeVisible()
  })
})

describe('the approval queue is offered on the rights its endpoint accepts', () => {
  it('withholds it from a QC inspector, whose qc.decide the endpoint refuses', async () => {
    // `GET /audit/approvals` is guarded on every `*.approve` / `*_approve`
    // right and nothing else. The hand-written list used to include
    // `qc.decide`, so an inspector saw the panel, then a 403 and a toast.
    const { fetch } = renderDashboard([P.QC_DECIDE])

    expect(await screen.findByText('Nothing to show on your dashboard')).toBeVisible()
    expect(screen.queryByText('Needs your attention')).toBeNull()
    expect(fetch.calls.some((call) => call.path === '/audit/approvals')).toBe(false)
  })

  it('offers it to a payment approver, whom the list used to omit', async () => {
    const { fetch } = renderDashboard([P.PAYMENT_APPROVE], {
      'GET /audit/approvals': page([]),
    })

    expect(await screen.findByText('Needs your attention')).toBeVisible()
    expect(await screen.findByText('Nothing waiting on you')).toBeVisible()
    await waitFor(() => fetch.requestFor('GET', '/audit/approvals'))
    // No "All approvals" link: nothing lists the queue beyond this preview,
    // and the audit trail it used to point at is gated on a right an approver
    // need not hold.
    expect(screen.queryByRole('link', { name: /all approvals/i })).toBeNull()
  })

  it('links a queued document to the screen the audit trail uses for its type', async () => {
    // One entity-type-to-route map, shared with the audit screen. The dashboard
    // kept a copy and it had fallen ten record types behind: a payment linked
    // to /finance from the audit trail and to a generic entity-history URL here.
    renderDashboard([P.PAYMENT_APPROVE], {
      'GET /audit/approvals': page([
        {
          id: 'apr-1',
          entity_type: 'payment',
          entity_id: '01a00000-0000-7000-8000-0000000000a1',
          entity_code: 'PAY-0001',
          requested_action: 'approve',
          reason: null,
          status: 'PENDING',
          created_at: '2026-09-04T05:00:00Z',
        },
      ]),
    })

    expect(await screen.findByRole('link', { name: /PAY-0001/ })).toHaveAttribute(
      'href',
      '/finance',
    )
  })
})

describe('the finance and operations tiles', () => {
  const FINANCE_SUMMARY = {
    ...SUMMARY,
    open_sales_orders: 3,
    pending_purchase_orders: 11,
    purchase_orders_awaiting_approval: 4,
    inbound_purchase_orders_late: 5,
    payables_due_amount: '50000.00',
    payables_overdue_amount: '25000.00',
    receivables_amount: '900000.00',
    invoiced_sales_mtd: '400000.00',
    collections_mtd: '150000.00',
    mrp_shortage_count: 7,
    job_work_overdue: 1,
  }

  const allRights = [
    P.DASHBOARD_VIEW,
    P.PURCHASE_ORDER_VIEW,
    P.VENDOR_INVOICE_VIEW,
    P.SALES_INVOICE_VIEW,
    P.PAYMENT_VIEW,
    P.MRP_VIEW,
    P.JOB_WORK_VIEW,
  ]

  it('labels each purchase figure for what it counts', async () => {
    // The tile called "POs awaiting approval" counted every open order, so a
    // committed pipeline read as a decision queue. The two counts are separate
    // now and the labels say which is which.
    renderDashboard(allRights, {
      'GET /reports/dashboard': ok(FINANCE_SUMMARY),
    })

    expect(await screen.findByText('Open purchase orders')).toBeVisible()
    expect(screen.getByText('POs awaiting approval')).toBeVisible()
    // The labels exist while the payload is still in flight (skeletons); the
    // figures prove it has arrived.
    expect(await screen.findByText('11')).toBeVisible()
    expect(screen.getByText('4')).toBeVisible()
  })

  it('drops a tile whose figure the API withheld, instead of drawing a zero', async () => {
    // The permission is held, but the payload says `null`: the API decided the
    // figure is not this caller's to read. Zero would be a business fact; the
    // tile must disappear.
    renderDashboard([P.DASHBOARD_VIEW, P.PURCHASE_ORDER_VIEW, P.VENDOR_INVOICE_VIEW], {
      'GET /reports/dashboard': ok({ ...FINANCE_SUMMARY, payables_due_amount: null }),
    })

    expect(await screen.findByText('POs awaiting approval')).toBeVisible()
    // Wait for the payload: while it is in flight every permitted tile is a
    // skeleton, so an immediate absence check would pass for the wrong reason.
    expect(await screen.findByText('₹25K')).toBeVisible()
    expect(screen.queryByText('Payables due')).toBeNull()
    expect(screen.getByText('Payables overdue')).toBeVisible()
  })

  it('draws a sparkline for each series the API returned, and none for the rest', async () => {
    renderDashboard(allRights, {
      'GET /reports/dashboard': ok(FINANCE_SUMMARY),
      'GET /reports/dashboard/trends': ok({
        date_from: '2026-09-03',
        date_to: '2026-09-04',
        series: [
          {
            series: 'sales_invoiced',
            points: [
              { day: '2026-09-03', value: '100.00' },
              { day: '2026-09-04', value: '300.00' },
            ],
          },
          {
            series: 'dispatches',
            points: [
              { day: '2026-09-03', value: '0.00' },
              { day: '2026-09-04', value: '3.50' },
            ],
          },
          {
            series: 'production_output',
            points: [
              { day: '2026-09-03', value: '1.25' },
              { day: '2026-09-04', value: '0.00' },
            ],
          },
        ],
        generated_at: '2026-09-04T06:15:00Z',
      }),
    })

    expect(await screen.findByText('Last 30 days')).toBeVisible()
    expect(
      await screen.findByRole('img', { name: /^Sales invoiced: .* over the last 2 days$/ }),
    ).toBeVisible()
    expect(screen.getByRole('img', { name: 'Dispatches: 3.5 over the last 2 days' })).toBeVisible()
    expect(
      screen.getByRole('img', { name: 'Production output: 1.25 over the last 2 days' }),
    ).toBeVisible()
    // Collections was not returned, so it is absent rather than drawn flat.
    expect(screen.queryByRole('img', { name: /^Collections:/ })).toBeNull()
  })
})
