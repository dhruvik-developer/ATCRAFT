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

import { screen } from '@testing-library/react'
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

/** The `data` block of the 202, exactly as `api.getPage` hands it back. */
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
        [`GET ${REPORT_PATH}`]: ok(ACCEPTED),
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
