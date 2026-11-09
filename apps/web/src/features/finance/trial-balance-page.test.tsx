/**
 * Trial balance.
 *
 * The screen exists to prove one fact - the closing debit balances equal the
 * closing credit balances - and to say so in words when they do not. The three
 * properties pinned here are the ones that make it trustworthy: the period
 * printed is the one the server used, a `balanced: false` answer is called out
 * as an inconsistency rather than a formatting quirk, and the XLSX is a job.
 */

import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, renderScreen } from '@/test/harness'

import TrialBalancePage from './trial-balance-page'

const PATH = '/finance/trial-balance'

const REPORT = {
  as_of: '2026-09-30',
  period_start: '2026-04-01',
  period_end: '2026-09-30',
  groups: [
    {
      account_type: 'ASSET',
      rows: [
        {
          account_id: 'ac-receivable',
          account_code: '1200',
          account_name: 'Accounts receivable',
          account_type: 'ASSET',
          opening_balance: '1000.00',
          period_debit: '500.00',
          period_credit: '0.00',
          closing_balance: '1500.00',
        },
      ],
      opening_balance: '1000.00',
      period_debit: '500.00',
      period_credit: '0.00',
      closing_balance: '1500.00',
    },
    {
      account_type: 'LIABILITY',
      rows: [
        {
          account_id: 'ac-payable',
          account_code: '2100',
          account_name: 'Accounts payable',
          account_type: 'LIABILITY',
          opening_balance: '-1000.00',
          period_debit: '0.00',
          period_credit: '-500.00',
          closing_balance: '-1500.00',
        },
      ],
      opening_balance: '-1000.00',
      period_debit: '0.00',
      period_credit: '-500.00',
      closing_balance: '-1500.00',
    },
  ],
  total_opening_balance: '0.00',
  total_period_debit: '500.00',
  total_period_credit: '-500.00',
  total_debit_balance: '1500.00',
  total_credit_balance: '-1500.00',
  balanced: false,
}

const JOB_ID = 'jb000000-0000-7000-8000-000000000003'
const JOB_PATH = `/reports/jobs/${JOB_ID}`
const ACCEPTED = { job_id: JOB_ID, status: 'queued', status_url: `/api/v1${JOB_PATH}` }

describe('the trial balance', () => {
  it('prints the period the server used, groups by class and names an imbalance', async () => {
    renderScreen(<TrialBalancePage />, {
      permissions: [P.LEDGER_VIEW],
      route: '/finance/trial-balance?as_of=2026-09-30',
      routes: { [`GET ${PATH}`]: ok(REPORT) },
    })

    expect(await screen.findByText('Accounts receivable')).toBeVisible()
    expect(screen.getByText('Accounts payable')).toBeVisible()
    expect(screen.getByText('Asset')).toBeVisible()
    expect(screen.getByText('Liability')).toBeVisible()
    // The client date and the server period can disagree if a financial year
    // has been opened or closed since; the server's answer is what is printed.
    expect(screen.getByText(/Period 01 Apr 2026 to 30 Sep 2026/)).toBeVisible()
    expect(screen.getByText('The trial balance does not balance')).toBeVisible()
  })

  it("says a failed load through the API's reason and offers to load it again", async () => {
    let attempts = 0
    const { user } = renderScreen(<TrialBalancePage />, {
      permissions: [P.LEDGER_VIEW],
      route: '/finance/trial-balance?as_of=2026-09-30',
      routes: {
        [`GET ${PATH}`]: () => {
          attempts += 1
          return attempts === 1
            ? fail(503, 'SERVICE_UNAVAILABLE', 'The ledger is busy.')
            : ok(REPORT)
        },
      },
    })

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText('Could not load the trial balance')).toBeVisible()
    expect(within(alert).getByText('The ledger is busy. (reference test)')).toBeVisible()
    await user.click(within(alert).getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('Accounts receivable')).toBeVisible()
  })

  it('does not warn when the two sides agree', async () => {
    renderScreen(<TrialBalancePage />, {
      permissions: [P.LEDGER_VIEW],
      route: '/finance/trial-balance?as_of=2026-09-30',
      routes: {
        [`GET ${PATH}`]: ok({
          ...REPORT,
          total_credit_balance: '1500.00',
          balanced: true,
        }),
      },
    })

    expect(await screen.findByText('Accounts receivable')).toBeVisible()
    expect(screen.queryByText('The trial balance does not balance')).toBeNull()
  })

  it('exports through the job pipeline, not as an inline file', async () => {
    const asked: URL[] = []
    const { user } = renderScreen(<TrialBalancePage />, {
      permissions: [P.LEDGER_VIEW, P.REPORT_EXPORT],
      route: '/finance/trial-balance?as_of=2026-09-30',
      routes: {
        [`GET ${PATH}`]: ok(REPORT),
        'POST /finance/trial-balance/export': ({ url }) => {
          asked.push(url)
          return ok(ACCEPTED)
        },
        [`GET ${JOB_PATH}`]: ok({
          id: JOB_ID,
          report_key: 'TRIAL_BALANCE',
          status: 'COMPLETED',
          parameters: null,
          row_count: 2,
          generated_at: '2026-09-30T09:00:00Z',
          expires_at: '2026-10-01T09:00:00Z',
          document_id: null,
          download_url: '/api/v1/documents/doc-tb/download',
          error_message: null,
          created_at: '2026-09-30T08:59:00Z',
        }),
      },
    })

    await user.click(await screen.findByRole('button', { name: 'Export XLSX' }))

    expect(await screen.findByText('Trial balance is ready')).toBeVisible()
    expect(asked).toHaveLength(1)
    // The export must cover the date on screen, not today's date.
    expect(asked[0]!.searchParams.get('as_of')).toBe('2026-09-30')
  })

  it('offers no export to an account without report.export', async () => {
    renderScreen(<TrialBalancePage />, {
      permissions: [P.LEDGER_VIEW],
      route: '/finance/trial-balance?as_of=2026-09-30',
      routes: { [`GET ${PATH}`]: ok(REPORT) },
    })

    expect(await screen.findByText('Accounts receivable')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Export XLSX' })).toBeNull()
  })

  it('does not ask for the ledger without ledger.view', async () => {
    const { fetch } = renderScreen(<TrialBalancePage />, {
      permissions: [],
      route: '/finance/trial-balance',
    })

    expect(await screen.findByText('The ledger is not visible to this account')).toBeVisible()
    expect(fetch.calls.some((call) => call.path === PATH)).toBe(false)
  })
})
