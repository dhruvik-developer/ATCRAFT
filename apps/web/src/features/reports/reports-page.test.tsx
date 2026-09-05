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
