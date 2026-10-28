/**
 * The report runner, and what "Run report" does the second time.
 *
 * The query key is built from the URL, so submitting the form with the same
 * parameters leaves the key unchanged and TanStack Query has nothing to do: no
 * request, no strip, no table movement, no message. For a report that
 * escalated to a background job it is worse - dismissing the strip sets the
 * job back to idle while `list.data` still holds the accepted envelope, so the
 * screen rendered neither a strip nor a table. Both are pinned here.
 */

import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import type { ReportJob } from './report-jobs'
import ReportsPage from './reports-page'
import type { ReportDescriptor } from './reports-types'

const DESCRIPTOR: ReportDescriptor = {
  key: 'STOCK_AVAILABILITY',
  title: 'Stock availability',
  description: 'On hand, reserved and available, by item and warehouse.',
  requires_period: false,
  columns: [
    { name: 'item_id', label: 'Item id', kind: 'id' },
    { name: 'item_code', label: 'Item', kind: 'text' },
    { name: 'available_quantity', label: 'Available', kind: 'number' },
  ],
}

const REPORT_PATH = '/reports/stock-availability'
const JOB_ID = 'jb000000-0000-7000-8000-000000000001'
const JOB_PATH = `/reports/jobs/${JOB_ID}`
const DOCUMENT_ID = 'dc000000-0000-7000-8000-000000000001'

/** The `data` block of the 202 returned by `api.getPageOrAccepted`. */
const ACCEPTED = { job_id: JOB_ID, status: 'queued', status_url: `/api/v1${JOB_PATH}` }

function job(status: string): ReportJob {
  return {
    id: JOB_ID,
    report_key: 'STOCK_AVAILABILITY',
    status,
    parameters: null,
    row_count: 12000,
    generated_at: '2026-09-05T09:00:00Z',
    expires_at: '2026-09-06T09:00:00Z',
    document_id: DOCUMENT_ID,
    download_url: `/api/v1/documents/${DOCUMENT_ID}/download`,
    error_message: null,
    created_at: '2026-09-05T08:59:00Z',
  }
}

const routes = {
  'GET /reports/catalogue': ok([DESCRIPTOR]),
  // The warehouse filter loads its options as soon as the runner mounts.
  'GET /warehouses': page([]),
  'GET /routing/work-centers': page([]),
  'GET /organization/cost-centres': page([]),
}

describe('running the same report twice', () => {
  it('fetches again when Run report is pressed with the parameters unchanged', async () => {
    const { user, fetch } = renderScreen(<ReportsPage />, {
      permissions: [P.REPORT_VIEW],
      route: '/reports?report=STOCK_AVAILABILITY&run=1',
      routes: {
        ...routes,
        [`GET ${REPORT_PATH}`]: page([
          { item_id: 'i1', item_code: 'BR-12', available_quantity: '4.0000' },
        ]),
      },
    })

    expect(await screen.findByText('BR-12')).toBeInTheDocument()
    const runs = () => fetch.calls.filter((call) => call.path === REPORT_PATH).length
    expect(runs()).toBe(1)

    await user.click(screen.getByRole('button', { name: 'Run report' }))

    await expect.poll(runs).toBe(2)
  })

  it('brings a dismissed download back on the next run instead of a blank screen', async () => {
    const { user } = renderScreen(<ReportsPage />, {
      permissions: [P.REPORT_VIEW],
      route: '/reports?report=STOCK_AVAILABILITY&run=1',
      routes: {
        ...routes,
        [`GET ${REPORT_PATH}`]: { ...ok(ACCEPTED), __status: 202 },
        [`GET ${JOB_PATH}`]: ok(job('COMPLETED')),
      },
    })

    expect(await screen.findByText('Stock availability is ready')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Dismiss' }))
    // Honest about what happened, rather than nothing at all.
    expect(await screen.findByText('The download was dismissed')).toBeInTheDocument()
    expect(screen.queryByText('Stock availability is ready')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Run report' }))

    expect(await screen.findByText('Stock availability is ready')).toBeInTheDocument()
    expect(screen.queryByText('The download was dismissed')).not.toBeInTheDocument()
  })
})

describe('the cost centre statement', () => {
  /**
   * Served at the kebab-case of its key like every report, over a period it
   * cannot run without. Net is debit minus credit on every row - positive is
   * cost, negative is income - so an income account shows a negative net.
   */
  const STATEMENT: ReportDescriptor = {
    key: 'COST_CENTRE_EXPENSE',
    title: 'Cost centre statement',
    description: 'Posted income and expense per cost centre and account over the period.',
    requires_period: true,
    columns: [
      { name: 'cost_centre_id', label: 'Cost centre id', kind: 'id' },
      { name: 'cost_centre_code', label: 'Cost centre code', kind: 'text' },
      { name: 'account_id', label: 'Account id', kind: 'id' },
      { name: 'account_code', label: 'Account code', kind: 'text' },
      { name: 'debit_amount', label: 'Debit amount', kind: 'number' },
      { name: 'credit_amount', label: 'Credit amount', kind: 'number' },
      { name: 'net_amount', label: 'Net amount', kind: 'number' },
    ],
  }

  it('runs over its period and shows a net of cost and income as money', async () => {
    const asked: URL[] = []
    renderScreen(<ReportsPage />, {
      permissions: [P.REPORT_VIEW],
      route: '/reports?report=COST_CENTRE_EXPENSE&run=1&date_from=2026-04-01&date_to=2026-09-30',
      routes: {
        ...routes,
        'GET /reports/catalogue': ok([STATEMENT]),
        'GET /reports/cost-centre-expense': ({ url }) => {
          asked.push(url)
          return page([
            {
              cost_centre_id: 'cc-mach',
              cost_centre_code: 'CC-MACH',
              account_id: 'ac-purchase',
              account_code: '5000',
              debit_amount: '12500.00',
              credit_amount: '0.00',
              net_amount: '12500.00',
            },
            {
              cost_centre_id: 'cc-mach',
              cost_centre_code: 'CC-MACH',
              account_id: 'ac-scrap-sales',
              account_code: '4100',
              debit_amount: '0.00',
              credit_amount: '3000.00',
              net_amount: '-3000.00',
            },
          ])
        },
      },
    })

    expect(await screen.findByText('5000')).toBeVisible()
    expect(screen.getAllByText('₹12,500.00')).toHaveLength(2)
    expect(screen.getByText('-₹3,000.00')).toBeVisible()
    expect(asked[0]!.searchParams.get('date_from')).toBe('2026-04-01')
    expect(asked[0]!.searchParams.get('date_to')).toBe('2026-09-30')
  })
})

describe('production cost filters', () => {
  const PRODUCTION_COST: ReportDescriptor = {
    key: 'PRODUCTION_COST',
    title: 'Production cost',
    description: 'Closed production order costs.',
    requires_period: true,
    columns: [
      { name: 'actual_unit_cost', label: 'Actual unit cost', kind: 'number' },
      { name: 'standard_unit_cost', label: 'Standard unit cost', kind: 'number' },
      { name: 'total_variance', label: 'Total variance', kind: 'number' },
    ],
  }

  it('sends the selected work centre and cost centre to the report route', async () => {
    const asked: URL[] = []
    const { user } = renderScreen(<ReportsPage />, {
      permissions: [P.REPORT_VIEW, P.ITEM_COST_VIEW, P.WORK_CENTER_VIEW, P.COST_CENTRE_VIEW],
      route: '/reports?report=PRODUCTION_COST&date_from=2026-09-01&date_to=2026-09-30',
      routes: {
        ...routes,
        'GET /reports/catalogue': ok([PRODUCTION_COST]),
        'GET /routing/work-centers': page([
          {
            id: 'wc-mill',
            code: 'WC-MILL',
            name: 'Milling',
            description: null,
            plant_id: null,
            department_id: null,
            cost_centre_id: null,
            capacity_per_hour: '0',
            cost_per_hour: '0',
            setup_cost: '0',
            working_hours_per_day: '8',
            is_active: true,
            created_at: '2026-09-01T00:00:00Z',
            updated_at: '2026-09-01T00:00:00Z',
          },
        ]),
        'GET /organization/cost-centres': page([
          {
            id: 'cc-production',
            code: 'CC-PROD',
            name: 'Production',
            cost_centre_type: 'PRODUCTION',
            parent_id: null,
            plant_id: null,
            head_user_id: null,
            description: null,
            is_active: true,
            created_at: '2026-09-01T00:00:00Z',
            updated_at: '2026-09-01T00:00:00Z',
            deleted_at: null,
          },
        ]),
        'GET /reports/production-cost': ({ url }) => {
          asked.push(url)
          return page([])
        },
      },
    })

    await user.click(await screen.findByRole('combobox', { name: 'Work centre' }))
    await user.click(await screen.findByRole('option', { name: 'WC-MILL - Milling' }))
    await user.click(await screen.findByRole('combobox', { name: 'Cost centre' }))
    await user.click(await screen.findByRole('option', { name: /CC-PROD/ }))
    await user.click(screen.getByRole('button', { name: 'Run report' }))
    await screen.findByText('No rows for these parameters')

    expect(asked).toHaveLength(1)
    expect(asked[0]!.searchParams.get('work_center_id')).toBe('wc-mill')
    expect(asked[0]!.searchParams.get('cost_centre_id')).toBe('cc-production')
  })
})

describe('actual product profitability', () => {
  const PROFITABILITY: ReportDescriptor = {
    key: 'PRODUCT_PROFITABILITY',
    title: 'Product profitability',
    description: 'Invoice and credit revenue with matched FIFO costs.',
    requires_period: true,
    columns: [
      { name: 'item_id', label: 'Item id', kind: 'id' },
      { name: 'item_code', label: 'Item', kind: 'text' },
      { name: 'revenue_amount', label: 'Revenue amount', kind: 'number' },
      { name: 'cost_amount', label: 'Cost amount', kind: 'number' },
      { name: 'margin_amount', label: 'Margin amount', kind: 'number' },
      { name: 'margin_percent', label: 'Margin percent', kind: 'number' },
    ],
  }
  const profitabilityPath = '/reports/product-profitability'
  const profitabilityRoute =
    '/reports?report=PRODUCT_PROFITABILITY&run=1&date_from=2026-09-01&date_to=2026-09-30'

  it('explains the date basis and flags provisional, unpriced and unmatched rows', async () => {
    const { fetch } = renderScreen(<ReportsPage />, {
      permissions: [P.REPORT_VIEW, P.ITEM_COST_VIEW],
      route: profitabilityRoute,
      routes: {
        ...routes,
        'GET /reports/catalogue': ok([PROFITABILITY]),
        [`GET ${profitabilityPath}`]: page([
          {
            item_id: 'i-pending',
            item_code: 'PENDING-COST',
            revenue_amount: '500.00',
            cost_amount: null,
            margin_amount: null,
            margin_percent: null,
            cost_pending: true,
            has_provisional_revenue: false,
            unpriced_quantity: '0',
          },
          {
            item_id: 'i-unpriced',
            item_code: 'UNPRICED',
            revenue_amount: null,
            cost_amount: '0.00',
            margin_amount: null,
            margin_percent: null,
            cost_pending: false,
            has_provisional_revenue: true,
            unpriced_quantity: '0.0001',
          },
        ]),
      },
    })

    expect(await screen.findByText('PENDING-COST')).toBeVisible()
    expect(screen.getByText('Invoice and credit date basis')).toBeVisible()
    expect(screen.getByText(/FIFO costs follow the billed quantities/)).toBeVisible()
    expect(screen.getByText('Some results are provisional or incomplete')).toBeVisible()
    expect(screen.getByText(/Uninvoiced dispatch amounts are provisional estimates/)).toBeVisible()
    expect(screen.getByText(/Some billed quantities have no matched FIFO cost yet/)).toBeVisible()
    expect(screen.getByText(/Some uninvoiced quantities have no agreed price/)).toBeVisible()
    const pendingRow = within(screen.getByText('PENDING-COST').closest('tr')!)
    expect(pendingRow.getAllByText('Unavailable')).toHaveLength(3)
    const unpricedRow = within(screen.getByText('UNPRICED').closest('tr')!)
    expect(unpricedRow.getAllByText('Unavailable')).toHaveLength(3)
    expect(unpricedRow.getByText('₹0.00')).toBeVisible()
    expect(fetch.calls.filter((call) => call.path === profitabilityPath)).toHaveLength(1)
  })

  it('keeps known zero costs and margins distinct from unavailable values', async () => {
    renderScreen(<ReportsPage />, {
      permissions: [P.REPORT_VIEW, P.ITEM_COST_VIEW],
      route: profitabilityRoute,
      routes: {
        ...routes,
        'GET /reports/catalogue': ok([PROFITABILITY]),
        [`GET ${profitabilityPath}`]: page([
          {
            item_id: 'i-zero',
            item_code: 'ZERO-COST',
            revenue_amount: '0.00',
            cost_amount: '0.00',
            margin_amount: '0.00',
            margin_percent: '0',
            cost_pending: false,
            has_provisional_revenue: false,
            unpriced_quantity: '0.0000',
          },
        ]),
      },
    })

    const row = within((await screen.findByText('ZERO-COST')).closest('tr')!)
    expect(row.getAllByText('₹0.00')).toHaveLength(3)
    expect(row.getByText('0.00%')).toBeVisible()
    expect(screen.queryByText('Unavailable')).not.toBeInTheDocument()
    expect(screen.queryByText('Some results are provisional or incomplete')).not.toBeInTheDocument()
  })

  it('does not request profitability when the permission-filtered catalogue excludes it', async () => {
    const { fetch } = renderScreen(<ReportsPage />, {
      permissions: [P.REPORT_VIEW],
      route: profitabilityRoute,
      routes,
    })

    expect(await screen.findByText('That report is not available')).toBeVisible()
    expect(screen.queryByText('Invoice and credit date basis')).not.toBeInTheDocument()
    expect(fetch.calls.some((call) => call.path === profitabilityPath)).toBe(false)
  })

  it('retains the ordinary missing-value presentation for other reports', async () => {
    renderScreen(<ReportsPage />, {
      permissions: [P.REPORT_VIEW],
      route: '/reports?report=STOCK_AVAILABILITY&run=1',
      routes: {
        ...routes,
        [`GET ${REPORT_PATH}`]: page([
          { item_id: 'i-missing', item_code: 'MISSING-STOCK', available_quantity: null },
        ]),
      },
    })

    const row = within((await screen.findByText('MISSING-STOCK')).closest('tr')!)
    expect(row.getByText('-')).toBeVisible()
    expect(screen.queryByText('Unavailable')).not.toBeInTheDocument()
    expect(screen.queryByText('Invoice and credit date basis')).not.toBeInTheDocument()
  })
})
