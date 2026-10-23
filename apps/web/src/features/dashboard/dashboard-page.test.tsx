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

import { cursorPage, ok, page, renderScreen, type RouteTable } from '@/test/harness'
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
  return renderScreen(<DashboardPage />, { permissions, routes })
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

  it('never asks for the summary without the right, and offers no tile that could only show a dash', async () => {
    // Sign-in lands on `/` by default, so an account without `dashboard.view`
    // used to collect a 403 and a "Not permitted" toast on every visit.
    const { fetch } = renderDashboard([P.SALES_ORDER_VIEW])

    expect(await screen.findByText('Nothing to show on your dashboard')).toBeVisible()
    expect(screen.queryByText('Open sales orders')).toBeNull()
    expect(fetch.calls.some((call) => call.path === '/reports/dashboard')).toBe(false)
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
