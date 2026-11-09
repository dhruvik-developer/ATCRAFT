import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { Toaster } from '@/components/ui/toaster'
import { fail, ok, renderScreen } from '@/test/harness'

import { ReportJobPanel } from './job-panel'
import {
  useReportJob,
  type ReportJob,
  type ReportJobController,
  type ReportJobPhase,
} from './report-jobs'

function panel(
  overrides: Partial<ReportJob> = {},
  phase: ReportJobPhase = 'ready',
  label = 'Product profitability',
) {
  const job: ReportJob = {
    id: 'job-profitability',
    report_key: 'PRODUCT_PROFITABILITY',
    status: 'COMPLETED',
    parameters: null,
    row_count: 12,
    generated_at: '2026-09-30T12:00:00Z',
    expires_at: '2026-10-01T12:00:00Z',
    document_id: 'document-profitability',
    download_url: '/api/v1/reports/jobs/job-profitability/download',
    error_message: null,
    created_at: '2026-09-30T11:59:00Z',
    ...overrides,
  }
  const controller: ReportJobController = {
    phase,
    job,
    error: null,
    start: vi.fn(),
    reset: vi.fn(),
    retry: vi.fn(),
    isRetrying: false,
    download: vi.fn(),
    isDownloading: false,
  }
  render(<ReportJobPanel controller={controller} label={label} />)
  return controller
}

describe('profitability export calculation basis', () => {
  it.each([null, {}, { calculation_version: 1 }])(
    'labels a completed older calculation and preserves its download (%j)',
    async (parameters) => {
      const user = userEvent.setup()
      const controller = panel({ parameters })

      expect(screen.getByText('Historical standard-cost calculation')).toBeVisible()
      expect(
        screen.getByText(/Run Product profitability again for invoice and credit dates/),
      ).toBeVisible()
      expect(screen.getByText('12 rows.')).toBeVisible()
      await user.click(screen.getByRole('button', { name: 'Download' }))
      expect(controller.download).toHaveBeenCalledOnce()
      await user.click(screen.getByRole('button', { name: 'Dismiss' }))
      expect(controller.reset).toHaveBeenCalledOnce()
    },
  )

  it('keeps a current actual-profitability export in the normal ready flow', () => {
    panel({ parameters: { calculation_version: 2 } })

    expect(screen.getByText('Product profitability is ready')).toBeVisible()
    expect(screen.queryByText('Historical standard-cost calculation')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled()
  })

  it('does not label other report exports as old profitability calculations', () => {
    panel({ report_key: 'STOCK_AVAILABILITY' })

    expect(screen.getByText('Product profitability is ready')).toBeVisible()
    expect(screen.queryByText('Historical standard-cost calculation')).not.toBeInTheDocument()
  })

  it('does not present an old queued job as a completed historical file', () => {
    panel({ status: 'QUEUED', document_id: null, download_url: null }, 'queued')

    expect(screen.getByText('Product profitability is queued')).toBeVisible()
    expect(screen.queryByText('Historical standard-cost calculation')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Download' })).not.toBeInTheDocument()
  })
})

describe('a status read that fails', () => {
  /** A screen that queues an export and watches it through the shared controller. */
  function WatchedExport() {
    const job = useReportJob()
    return (
      <>
        <button
          type="button"
          onClick={() =>
            job.start({
              job_id: 'job-9',
              status: 'queued',
              status_url: '/api/v1/reports/jobs/job-9',
            })
          }
        >
          Export
        </button>
        <ReportJobPanel controller={job} label="Packing list" />
      </>
    )
  }

  it('is said once, in the strip, and a retry reads the same job again', async () => {
    let unavailable = true
    const { user, fetch } = renderScreen(
      <>
        <WatchedExport />
        <Toaster />
      </>,
      {
        errorNets: true,
        routes: {
          'GET /reports/jobs/job-9': () =>
            unavailable
              ? fail(503, 'SERVICE_UNAVAILABLE', 'Worker status is unavailable')
              : ok({
                  id: 'job-9',
                  report_key: 'PACKING_LIST',
                  status: 'COMPLETED',
                  parameters: null,
                  row_count: 4,
                  generated_at: '2026-11-09T10:00:05Z',
                  expires_at: '2026-11-10T10:00:05Z',
                  document_id: 'doc-9',
                  download_url: '/api/v1/reports/jobs/job-9/download',
                  error_message: null,
                  created_at: '2026-11-09T10:00:00Z',
                }),
        },
      },
    )

    await user.click(screen.getByRole('button', { name: 'Export' }))

    const strip = await screen.findByText('Could not generate the packing list')
    expect(strip.closest('[role="alert"]')).toHaveTextContent('Worker status is unavailable')
    // The strip says it; the global net, which toasts a failed read nobody
    // shows, stays out - an error toast never dismisses itself.
    const toasts = within(screen.getByRole('region', { name: 'Notifications' }))
    expect(toasts.queryAllByRole('alert')).toHaveLength(0)
    expect(screen.getAllByRole('alert')).toHaveLength(1)

    unavailable = false
    await user.click(screen.getByRole('button', { name: 'Retry status' }))
    expect(await screen.findByText('Packing list is ready')).toBeVisible()
    expect(fetch.calls.filter((call) => call.path === '/reports/jobs/job-9')).toHaveLength(2)
  })
})

describe('a failed export', () => {
  it.each([
    ['Packing list', 'Could not generate the packing list'],
    ['GSTR-1 workbook', 'Could not generate the GSTR-1 workbook'],
  ])('headlines %s in the catalogue form', (label, headline) => {
    // It said "<label> could not be generated", unlike every other failure headline.
    panel({ status: 'FAILED', error_message: 'The export timed out.' }, 'failed', label)

    expect(screen.getByText(headline)).toBeVisible()
    expect(screen.getByText('The export timed out.')).toBeVisible()
  })
})
